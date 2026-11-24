"""LaTeX manuscript workspace (A01, CP9.2; decision 0012).

A paper's workspace is the folder that holds its manuscript source: the paper's
``manuscript_source_id`` names a source bound (``root_binding``) to the main ``.tex``
file inside an approved research root, and the folder containing that file is the
workspace. Files on disk stay the manuscript authority (decision 0008 A01.2):

* listings and reads are bounded and never follow links or reparse points;
* a write replaces a file atomically only while its bytes still have the digest the
  writer loaded (``base_sha256``); otherwise nothing is written;
* versions are recorded only by an explicit snapshot and automatically for every
  compile input set, through the ordinary import path, so a new manuscript version
  runs the anchor remap rules of decision 0007 item 7 in the same commit.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from prometheos.app import reads, sources
from prometheos.app.reads import Caller
from prometheos.domain.canonical import dumps_canonical, loads_internal
from prometheos.domain.ids import is_record_id, new_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.latex import structure
from prometheos.sources.paths import (
    VCS_DIRECTORIES,
    is_link_or_reparse,
    normalize_relative,
    open_verified,
    resolve_in_root,
)
from prometheos.store.project import ProjectStore

MAX_FILES = 500
MAX_DEPTH = 8
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_STRUCTURE_BYTES = 16 * 1024 * 1024
TEXT_SUFFIXES = frozenset(
    {".tex", ".bib", ".sty", ".cls", ".bst", ".bbx", ".cbx", ".txt", ".md", ".cfg", ".def"}
)
# Build products and editor droppings are not compile inputs (they are regenerated).
OUTPUT_SUFFIXES = frozenset(
    {
        ".aux",
        ".log",
        ".synctex",
        ".gz",
        ".fls",
        ".fdb_latexmk",
        ".out",
        ".toc",
        ".lof",
        ".lot",
        ".blg",
        ".bcf",
        ".nav",
        ".snm",
        ".xdv",
        ".bak",
        ".tmp",
        ".swp",
    }
)
OWNER_SCOPE_NOTE = (
    "paper not found at this scope (the workspace of a restricted or private paper needs "
    "owner scope)"
)
LIMITATIONS = [
    "Files on disk are the manuscript; unsaved editor buffers live only in the browser.",
    "Versions are recorded at explicit snapshots and for every compile input set, not on save.",
    "Structure data is read line by line from the source text; the compile is authoritative.",
]


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


# ----- resolution --------------------------------------------------------------------------


class Workspace:
    def __init__(
        self,
        *,
        paper: dict[str, Any],
        source: dict[str, Any],
        alias: str,
        root: Path,
        folder: str,
        main: str,
    ) -> None:
        self.paper = paper
        self.source = source
        self.alias = alias
        self.root = root
        self.folder = folder  # root-relative POSIX folder ("" for the root itself)
        self.main = main  # workspace-relative main file

    @property
    def paper_id(self) -> str:
        return str(self.paper["id"])

    @property
    def directory(self) -> Path:
        return self.root / self.folder if self.folder else self.root

    def root_relative(self, path: str) -> str:
        rel = normalize_relative(path)
        return f"{self.folder}/{rel}" if self.folder else rel

    def resolve(self, path: str) -> Any:
        resolved = resolve_in_root(self.root, self.root_relative(path))
        folder_prefix = (self.folder + "/") if self.folder else ""
        if folder_prefix and not resolved.relative.casefold().startswith(folder_prefix.casefold()):
            raise PrometheosError("policy_denied", "path escapes the workspace folder", {})
        return resolved

    def relative_of(self, resolved: Any) -> str:
        """Workspace-relative canonical path of a resolved root-relative path."""
        rel = str(resolved.relative)
        return rel[len(self.folder) + 1 :] if self.folder else rel

    def describe(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "paper_rev": self.paper["rev"],
            "title": self.paper["body"]["title"],
            "manuscript_source_id": self.source["id"],
            "root_alias": self.alias,
            "folder": self.folder,
            "main": self.main,
        }


def _record(conn: sqlite3.Connection, caller: Caller, record_id: str, kind: str) -> dict[str, Any]:
    if not is_record_id(record_id, kind):
        raise _invalid(f"malformed {kind} id", id=record_id)
    record = reads.current_record(conn, record_id)
    if (
        record is None
        or record["kind"] != kind
        or record["lifecycle"] == "deleted"
        or record["body"].get("purged")
        or not caller.may_see(record["sensitivity"])
    ):
        if kind == "paper":
            # Same answer for an absent paper and one above the caller's scope (no leak).
            raise PrometheosError("not_found", OWNER_SCOPE_NOTE, {"id": record_id})
        raise PrometheosError("not_found", f"{kind} not found", {"id": record_id})
    return record


def resolve(conn: sqlite3.Connection, project_id: str, caller: Caller, paper_id: str) -> Workspace:
    paper = _record(conn, caller, paper_id, "paper")
    source_id = paper["body"].get("manuscript_source_id")
    if source_id is None:
        raise PrometheosError(
            "validation_error",
            "this paper has no LaTeX workspace; bind one with `paper workspace bind`",
            {"paper_id": paper_id},
        )
    source = _record(conn, caller, source_id, "source")
    binding = source["body"].get("root_binding")
    if not binding or not str(binding["path"]).lower().endswith(".tex"):
        raise PrometheosError(
            "validation_error",
            "the paper's manuscript is not a .tex file in a research root; bind a workspace",
            {"paper_id": paper_id},
        )
    settings, _ = sources._settings(conn, project_id)
    root = sources.root_path(settings, binding["alias"])
    posix = PurePosixPath(binding["path"])
    folder = "" if str(posix.parent) == "." else str(posix.parent)
    return Workspace(
        paper=paper,
        source=source,
        alias=binding["alias"],
        root=root,
        folder=folder,
        main=posix.name,
    )


# ----- registered sources of the workspace -----------------------------------------------------


def _bindings(conn: sqlite3.Connection, alias: str) -> dict[str, dict[str, Any]]:
    """casefolded root-relative path -> {id, rev, sensitivity, retention, latest}."""
    found: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT r.id, r.current_rev, r.sensitivity, b.encoded FROM records r JOIN revisions v "
        "ON v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'source' AND r.lifecycle != 'deleted'"
    ):
        if row["encoded"] is None:
            continue
        body = loads_internal(row["encoded"])
        binding = body.get("root_binding")
        if binding and binding["alias"] == alias:
            found[str(binding["path"]).casefold()] = {
                "id": row["id"],
                "rev": row["current_rev"],
                "sensitivity": row["sensitivity"],
                "retention": body["retention"],
                "latest": None,
            }
    if found:
        by_id = {v["id"]: v for v in found.values()}
        for row in conn.execute(
            "SELECT r.id, r.sensitivity, b.encoded FROM records r JOIN revisions v ON "
            "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
            "WHERE r.kind = 'version' ORDER BY r.created_seq"
        ):
            if row["encoded"] is None:
                continue
            body = loads_internal(row["encoded"])
            owner = by_id.get(body.get("source_id"))
            if owner is not None:
                owner["latest"] = {
                    "version_id": row["id"],
                    "sha256": body["sha256"],
                    "sensitivity": row["sensitivity"],
                }
    return found


# ----- listing and reads -----------------------------------------------------------------------


def _sha_file(path: Path, limit: int = MAX_HASH_BYTES) -> str | None:
    if path.stat().st_size > limit:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_compile_input(path: str, main: str) -> bool:
    name = PurePosixPath(path)
    if name.suffix.lower() in OUTPUT_SUFFIXES:
        return False
    return not (
        len(name.parts) == 1 and name.suffix.lower() == ".pdf" and name.stem == Path(main).stem
    )


def walk(ws: Workspace) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Regular files under the workspace (hidden entries, links and reparse points skipped)."""
    files: list[dict[str, Any]] = []
    skipped = {"links": 0, "hidden": 0, "depth": 0, "over_limit": 0}
    base = ws.directory
    if not base.is_dir():
        raise PrometheosError("source_unavailable", "the workspace folder is unavailable", {})
    stack: list[tuple[Path, str, int]] = [(base, "", 0)]
    while stack:
        folder, prefix, depth = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                skipped["hidden"] += 1
                continue
            path = Path(entry.path)
            if is_link_or_reparse(path):
                skipped["links"] += 1
                continue
            rel = f"{prefix}{entry.name}"
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 >= MAX_DEPTH:
                    skipped["depth"] += 1
                    continue
                stack.append((path, rel + "/", depth + 1))
            elif stat.S_ISREG(info.st_mode):
                if len(files) >= MAX_FILES:
                    skipped["over_limit"] += 1
                    continue
                files.append({"path": rel, "size": info.st_size, "abs": path})
    files.sort(key=lambda f: f["path"].casefold())
    return files, skipped


FILE_ABOVE_SCOPE = (
    "owner scope is required: this workspace holds files registered above agent scope"
)


def _refuse_hidden(rel: str, caller: Caller | None = None, reading: bool = False) -> None:
    """Version-control metadata (``.git``, ``.hg``, ``.svn``) is refused for every caller: a
    clone's ``.git/config`` may hold a remote URL with a token. Other dot files and folders
    are never written, and only the owner may read one (e.g. ``.latexmkrc``; review cp9-a
    CP9-A-2 and round 2 R2-5)."""
    parts = rel.split("/")
    if any(part.casefold() in VCS_DIRECTORIES for part in parts):
        raise PrometheosError(
            "policy_denied", "version-control metadata is not read or written", {}
        )
    owner_read = reading and caller is not None and caller.scope == "owner"
    if not owner_read and any(part.startswith(".") for part in parts):
        raise PrometheosError(
            "policy_denied", "hidden files and folders are not read or written", {}
        )


def _above_scope(caller: Caller, source: dict[str, Any] | None) -> bool:
    return source is not None and not caller.may_see(source["sensitivity"])


def guard_file(
    caller: Caller,
    ws: Workspace,
    registered: dict[str, dict[str, Any]],
    rel: str,
    reading: bool = False,
) -> None:
    """The one per-file access rule for read, write, list, structure, snapshot, compile and
    evidence insertion (review cp9-a CP9-A-1, round 2 R2-1). ``rel`` must be the canonical
    workspace-relative path from :func:`paths.resolve_in_root` (real long names whether or
    not the file exists). Hidden paths follow :func:`_refuse_hidden`; a file whose
    registered source is above the caller's scope is refused without naming it."""
    _refuse_hidden(rel, caller, reading)
    if _above_scope(caller, registered.get(ws.root_relative(rel).casefold())):
        raise PrometheosError("policy_denied", FILE_ABOVE_SCOPE, {})


def guard_workspace(
    caller: Caller, ws: Workspace, registered: dict[str, dict[str, Any]], rels: list[str]
) -> None:
    """Whole-workspace operations (structure, snapshot, compile) refuse when any of the files
    they would use is above the caller's scope: leaving it out would mislead (undefined
    references, a compile of a different document)."""
    for rel in rels:
        guard_file(caller, ws, registered, rel)


def list_files(
    conn: sqlite3.Connection, project_id: str, caller: Caller, paper_id: str
) -> dict[str, Any]:
    ws = resolve(conn, project_id, caller, paper_id)
    found, skipped = walk(ws)
    registered = _bindings(conn, ws.alias)
    out = []
    for item in found:
        source = registered.get(ws.root_relative(item["path"]).casefold())
        if _above_scope(caller, source):
            continue  # review cp9-a CP9-A-3: nothing about it, not even a count
        sha = _sha_file(item["abs"])
        latest = source["latest"] if source else None
        if latest is None:
            state = "unversioned"
        else:
            state = "versioned" if latest["sha256"] == sha else "changed"
        out.append(
            {
                "path": item["path"],
                "size": item["size"],
                "sha256": sha,
                "text": PurePosixPath(item["path"]).suffix.lower() in TEXT_SUFFIXES,
                "compile_input": is_compile_input(item["path"], ws.main),
                "source_id": source["id"] if source else None,
                "latest_version_id": latest["version_id"] if latest else None,
                "state": state,
            }
        )
    return {
        "workspace": ws.describe(),
        "files": out,
        "skipped": skipped,
        "limits": {"max_files": MAX_FILES, "max_depth": MAX_DEPTH},
        "limitations": LIMITATIONS,
    }


def read_file(
    conn: sqlite3.Connection, project_id: str, caller: Caller, paper_id: str, path: str
) -> dict[str, Any]:
    ws = resolve(conn, project_id, caller, paper_id)
    _refuse_hidden(normalize_relative(path), caller, reading=True)
    resolved = ws.resolve(path)
    rel = ws.relative_of(resolved)
    guard_file(caller, ws, _bindings(conn, ws.alias), rel, reading=True)
    with open_verified(resolved) as handle:
        data = handle.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise PrometheosError(
            "budget_exceeded", "file is larger than the editor limit", {"limit": MAX_TEXT_BYTES}
        )
    sha = hashlib.sha256(data).hexdigest()
    text: str | None
    try:
        text = data.decode("utf-8") if b"\x00" not in data else None
    except UnicodeDecodeError:
        text = None
    return {
        "workspace": ws.describe(),
        "path": rel,
        "sha256": sha,
        "size": len(data),
        "text": text,
        "line_ending": "crlf" if text is not None and "\r\n" in text else "lf",
        "binary": text is None,
    }


def write_file(
    conn: sqlite3.Connection,
    project_id: str,
    caller: Caller,
    paper_id: str,
    path: str,
    text: str,
    base_sha256: str | None,
) -> dict[str, Any]:
    """Atomic replace guarded by the loaded digest; ``base_sha256=None`` creates a new file."""
    ws = resolve(conn, project_id, caller, paper_id)
    rel = normalize_relative(path)
    guard_file(caller, ws, _bindings(conn, ws.alias), rel)
    if PurePosixPath(rel).suffix.lower() not in TEXT_SUFFIXES:
        raise PrometheosError(
            "policy_denied",
            "the editor writes LaTeX, BibTeX and plain-text files only",
            {"suffix": PurePosixPath(rel).suffix},
        )
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise PrometheosError("budget_exceeded", "file is too large", {"limit": MAX_TEXT_BYTES})
    resolved = ws.resolve(rel)
    # The canonical name (real long names of every existing component) is what is checked.
    guard_file(caller, ws, _bindings(conn, ws.alias), ws.relative_of(resolved))
    target = resolved.path
    current = _current_digest(resolved)
    if current != base_sha256:
        raise PrometheosError(
            "revision_conflict",
            "the file changed on disk since it was loaded; nothing was written (your draft is "
            "kept by the editor)",
            {"path": rel, "disk_sha256": current, "base_sha256": base_sha256},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(target.parent):
        raise PrometheosError("policy_denied", "links and reparse points are refused", {})
    temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.prometheos-tmp"
    try:
        with open(temp, "xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Re-check immediately before the rename: an editor or Git may have written meanwhile.
        if _current_digest(resolved) != base_sha256:
            raise PrometheosError(
                "revision_conflict",
                "the file changed on disk while saving; nothing was written",
                {"path": rel},
            )
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "workspace": ws.describe(),
        "path": rel,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "created": base_sha256 is None,
    }


def _current_digest(resolved: Any) -> str | None:
    try:
        with open_verified(resolved) as handle:
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            return digest.hexdigest()
    except PrometheosError as exc:
        if exc.code == "source_unavailable":
            return None
        raise


# ----- structure (pure) ------------------------------------------------------------------------


STRUCTURE_SUFFIXES = (".tex", ".bib", ".sty", ".cls")


def text_files(ws: Workspace) -> dict[str, str]:
    files, _ = walk(ws)
    texts: dict[str, str] = {}
    budget = MAX_STRUCTURE_BYTES
    for item in files:
        if PurePosixPath(item["path"]).suffix.lower() not in STRUCTURE_SUFFIXES:
            continue
        if item["size"] > MAX_TEXT_BYTES or item["size"] > budget:
            continue
        try:
            with open_verified(ws.resolve(item["path"])) as handle:
                data = handle.read(MAX_TEXT_BYTES)
            texts[item["path"]] = data.decode("utf-8", "replace")
            budget -= len(data)
        except PrometheosError:
            continue
    return texts


def structure_query(
    conn: sqlite3.Connection, project_id: str, caller: Caller, paper_id: str
) -> dict[str, Any]:
    ws = resolve(conn, project_id, caller, paper_id)
    found, _ = walk(ws)
    guard_workspace(
        caller,
        ws,
        _bindings(conn, ws.alias),
        [f["path"] for f in found if PurePosixPath(f["path"]).suffix.lower() in STRUCTURE_SUFFIXES],
    )
    result = structure.analyze(text_files(ws))
    return {"workspace": ws.describe(), **result}


def anchors(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, paper_id: str
) -> list[dict[str, Any]]:
    """Anchors of the paper with their claims and the exact anchored lines (gutter data)."""
    from prometheos.app.apply import text_lines

    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT r.id, r.current_rev, r.sensitivity, b.encoded FROM records r JOIN revisions v ON "
        "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'anchor' AND r.lifecycle = 'active' ORDER BY r.id"
    ):
        if row["encoded"] is None or not caller.may_see(row["sensitivity"]):
            continue
        body = loads_internal(row["encoded"])
        if body["paper_id"] != paper_id:
            continue
        lines: list[str] | None = None
        version = reads.current_record(conn, body["manuscript_version_id"])
        if version is not None and body["locator"]["type"] == "text":
            data = version_bytes(conn, store_dir, version["body"])
            all_lines = text_lines(data) if data is not None else None
            if all_lines is not None:
                lines = all_lines[body["locator"]["start_line"] - 1 : body["locator"]["end_line"]]
        claims = []
        for ref in body["claim_refs"]:
            claim = reads.current_record(conn, ref["id"])
            if claim is not None and caller.may_see(claim["sensitivity"]):
                claims.append(
                    {
                        "id": ref["id"],
                        "rev": ref["rev"],
                        "statement": claim["body"].get("statement"),
                        "state": claim["body"].get("state"),
                    }
                )
        out.append(
            {
                "id": row["id"],
                "rev": row["current_rev"],
                "status": body["status"],
                "reason": body["reason"],
                "manuscript_version_id": body["manuscript_version_id"],
                "locator": body["locator"],
                "anchored_lines": lines,
                "claims": claims,
                "evidence": body["evidence"],
            }
        )
    return out


def version_bytes(conn: sqlite3.Connection, store_dir: Path, body: dict[str, Any]) -> bytes | None:
    if body.get("storage") == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        data = bytes(row["data"]) if row else None
    elif body.get("storage") == "media":
        try:
            data = (store_dir / "media" / str(body["storage_key"])).read_bytes()
        except OSError:
            return None
    else:
        return None
    if data is None or hashlib.sha256(data).hexdigest() != body["sha256"]:
        return None
    return data


# ----- bind and snapshot -----------------------------------------------------------------------


def _highest(*levels: str) -> str:
    return max(levels, key=lambda s: SENSITIVITY_ORDER[s])


def snapshot(
    store: ProjectStore,
    caller: Caller,
    paper_id: str,
    actor: str,
    paths: list[str] | None = None,
    inputs_only: bool = False,
) -> dict[str, Any]:
    """Record a version of every changed workspace file (or of ``paths``) in import plans.

    Returns ``{path: {source_id, version_id, sha256}}`` for every selected file, changed or
    not, so a compile can bind its exact inputs."""
    conn = store.connect(readonly=True)
    try:
        ws = resolve(conn, store.project_id, caller, paper_id)
        found, _ = walk(ws)
        registered = _bindings(conn, ws.alias)
    finally:
        conn.close()
    by_path = {f["path"]: f for f in found}
    if paths is not None:
        selected = []
        for given in paths:
            rel = normalize_relative(given)
            if rel not in by_path:
                raise PrometheosError("not_found", "file is not in the workspace", {})
            selected.append(rel)
    else:
        selected = [
            f["path"] for f in found if not inputs_only or is_compile_input(f["path"], ws.main)
        ]
    if not selected:
        raise _invalid("the workspace has no files to snapshot")
    guard_workspace(caller, ws, registered, selected)
    level = ws.paper["sensitivity"]
    for rel in selected:
        source = registered.get(ws.root_relative(rel).casefold())
        if source is not None:
            if source["retention"] != "copy":
                raise PrometheosError(
                    "validation_error",
                    "a workspace file is registered as reference-only; compile inputs must be "
                    "retained copies",
                    {"path": rel, "source_id": source["id"]},
                )
            level = _highest(level, source["sensitivity"])
    if caller.scope != "owner" and SENSITIVITY_ORDER[level] > 0:
        raise PrometheosError("not_found", OWNER_SCOPE_NOTE, {"id": paper_id})
    commits = []
    for start in range(0, len(selected), sources.MAX_PLAN_FILES):
        chunk = [ws.root_relative(p) for p in selected[start : start + sources.MAX_PLAN_FILES]]
        plan = sources.create_plan(
            store,
            root_alias=ws.alias,
            paths=chunk,
            retention="copy",
            sensitivity=level,
            description=f"LaTeX workspace file of paper {paper_id}",
            max_rank=caller.rank,
        )
        if any(item["action"] != "unchanged" for item in plan["items"]):
            commits.append(sources.commit_plan(store, plan["plan_id"], actor, max_rank=caller.rank))
    conn = store.connect(readonly=True)
    try:
        registered = _bindings(conn, ws.alias)
    finally:
        conn.close()
    versions: dict[str, dict[str, Any]] = {}
    for rel in selected:
        source = registered.get(ws.root_relative(rel).casefold())
        if source is None or source["latest"] is None:
            raise PrometheosError("io_error", "snapshot did not register a file", {"path": rel})
        versions[rel] = {
            "source_id": source["id"],
            "version_id": source["latest"]["version_id"],
            "sha256": source["latest"]["sha256"],
            "sensitivity": source["latest"]["sensitivity"],
        }
    return {
        "workspace": ws.describe(),
        "versions": versions,
        "commits": commits,
        "recorded": sum(
            1
            for c in commits
            for r in c.get("results", [])
            if str(r.get("id", "")).startswith("version_")
        ),
    }


def bind(
    store: ProjectStore,
    caller: Caller,
    paper_id: str,
    root_alias: str,
    main_path: str,
    actor: str,
    max_rank: int,
) -> dict[str, Any]:
    """Point the paper's manuscript at ``main_path`` (a .tex file in an approved root)."""
    from prometheos.app.apply import apply_request

    rel = normalize_relative(main_path)
    if not rel.lower().endswith(".tex"):
        raise _invalid("the main file must be a .tex file")
    conn = store.connect(readonly=True)
    try:
        paper = _record(conn, caller, paper_id, "paper")
        settings, _ = sources._settings(conn, store.project_id)
        root = sources.root_path(settings, root_alias)
        resolved = resolve_in_root(root, rel)
        with open_verified(resolved):
            pass
        existing = _bindings(conn, root_alias).get(resolved.relative.casefold())
    finally:
        conn.close()
    imported = None
    if existing is None or existing["latest"] is None:
        plan = sources.create_plan(
            store,
            root_alias=root_alias,
            paths=[resolved.relative],
            retention="copy",
            sensitivity=paper["sensitivity"],
            description=f"LaTeX workspace main file of paper {paper_id}",
            max_rank=caller.rank,
        )
        imported = sources.commit_plan(store, plan["plan_id"], actor, max_rank=caller.rank)
        conn = store.connect(readonly=True)
        try:
            existing = _bindings(conn, root_alias).get(resolved.relative.casefold())
        finally:
            conn.close()
    assert existing is not None
    if existing["retention"] != "copy":
        raise _invalid("the main file is registered as reference-only; it must be retained")
    if paper["body"].get("manuscript_source_id") == existing["id"]:
        return {"paper_id": paper_id, "manuscript_source_id": existing["id"], "commit": None}
    body = dict(paper["body"], manuscript_source_id=existing["id"])
    conn = store.connect(readonly=True)
    try:
        seq = store.project_seq(conn)
    finally:
        conn.close()
    request = {
        "schema_version": 1,
        "project_id": store.project_id,
        "request_id": new_request_id(),
        "expected_seq": seq,
        "operations": [
            {
                "op": "revise",
                "id": paper_id,
                "base_rev": paper["rev"],
                "sensitivity": paper["sensitivity"],
                "occurred_at": paper["occurred_at"],
                "body": body,
            }
        ],
    }
    commit = apply_request(store, dumps_canonical(request), actor, max_rank=max_rank)
    return {
        "paper_id": paper_id,
        "manuscript_source_id": existing["id"],
        "imported": imported,
        "commit": commit,
    }


# ----- insert citation or figure from recorded evidence -----------------------------------------

FIGURE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}


def _bib_text(value: str) -> str:
    """BibTeX-safe text: no braces or backslashes from record text."""
    return " ".join(value.replace("\\", " ").replace("{", "(").replace("}", ")").split())[:300]


def _relation(
    store: ProjectStore,
    version: dict[str, Any],
    paper: dict[str, Any],
    actor: str,
    max_rank: int,
) -> dict[str, Any] | None:
    from prometheos.app.apply import apply_request

    conn = store.connect(readonly=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM relations WHERE source_id = ? AND target_id = ? AND relation = "
            "'used_in' AND retired_seq IS NULL",
            (version["id"], paper["id"]),
        ).fetchone()
        seq = store.project_seq(conn)
    finally:
        conn.close()
    if exists:
        return None
    request = {
        "schema_version": 1,
        "project_id": store.project_id,
        "request_id": new_request_id(),
        "expected_seq": seq,
        "operations": [
            {
                "op": "relation_add",
                "source_ref": {"id": version["id"], "rev": version["rev"]},
                "target_ref": {"id": paper["id"], "rev": paper["rev"]},
                "relation": "used_in",
            }
        ],
    }
    return apply_request(store, dumps_canonical(request), actor, max_rank=max_rank)


def insert_evidence(
    store: ProjectStore,
    caller: Caller,
    paper_id: str,
    kind: str,
    version_id: str,
    actor: str,
    max_rank: int,
    bib_file: str | None = None,
) -> dict[str, Any]:
    """Write ordinary LaTeX for a recorded version and record ``used_in`` to the paper.

    ``cite``: ensure a ``@misc`` entry keyed ``prometheos-<8 hex>`` in a workspace ``.bib``
    file (digest-guarded write) and return ``\\cite{key}``. ``figure``: copy the retained
    image or PDF into ``figures/`` (never overwriting different bytes) and return a figure
    environment. The editor inserts the returned snippet at the cursor."""
    if kind not in ("cite", "figure"):
        raise _invalid("kind must be cite or figure")
    conn = store.connect(readonly=True)
    try:
        ws = resolve(conn, store.project_id, caller, paper_id)
        version = _record(conn, caller, version_id, "version")
        source = _record(conn, caller, version["body"]["source_id"], "source")
        data = version_bytes(conn, store.directory, version["body"]) if kind == "figure" else None
        files, _ = walk(ws)
    finally:
        conn.close()
    short = source["id"].split("_", 1)[1].replace("-", "")[:8]
    title = _bib_text(source["body"]["title"]) or "Recorded source"
    result: dict[str, Any] = {"kind": kind, "version_id": version_id, "source_id": source["id"]}
    if kind == "cite":
        key = f"prometheos-{short}"
        bibs = sorted(f["path"] for f in files if f["path"].lower().endswith(".bib"))
        target = normalize_relative(bib_file) if bib_file else (bibs[0] if bibs else "refs.bib")
        if not target.lower().endswith(".bib"):
            raise _invalid("citations are added to a .bib file")
        conn = store.connect(readonly=True)
        try:
            current = (
                read_file(conn, store.project_id, caller, paper_id, target)
                if (target in bibs)
                else None
            )
        finally:
            conn.close()
        text = (current or {}).get("text") or ""
        written = False
        if re.search(r"@\w+\s*\{\s*" + re.escape(key) + r"\s*,", text) is None:
            entry = (
                f"@misc{{{key},\n  title = {{{title}}},\n"
                f"  note = {{PrometheOS source {source['id']}, version {version_id}}}"
            )
            uri = source["body"].get("origin_uri")
            if uri and re.match(r"^https?://[^\s{}\\]+$", uri):
                entry += f",\n  howpublished = {{\\url{{{uri}}}}}"
            entry += "\n}\n"
            separator = (
                ""
                if not text or text.endswith("\n\n")
                else ("\n" if text.endswith("\n") else "\n\n")
            )
            conn = store.connect(readonly=True)
            try:
                write_file(
                    conn,
                    store.project_id,
                    caller,
                    paper_id,
                    target,
                    text + separator + entry,
                    current["sha256"] if current else None,
                )
            finally:
                conn.close()
            written = True
        result |= {
            "snippet": f"\\cite{{{key}}}",
            "key": key,
            "bib_file": target,
            "written": written,
        }
    else:
        mime = str(version["body"]["mime"]).split(";")[0].lower()
        suffix = FIGURE_MIMES.get(mime)
        if suffix is None or data is None:
            raise PrometheosError(
                "unsupported",
                "figures come from retained PNG, JPEG or PDF versions",
                {"mime": mime},
            )
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(source["body"]["title"]).stem).strip("-")
        stem = (stem or "figure")[:40].lower()
        digest = hashlib.sha256(data).hexdigest()
        existing = {f["path"].casefold(): f for f in files}
        name = f"figures/{stem}{suffix}"
        known = existing.get(name.casefold())
        if known is not None and _sha_file(known["abs"]) != digest:
            name = f"figures/{stem}-{digest[:8]}{suffix}"
            known = existing.get(name.casefold())
        written = False
        resolved = ws.resolve(name)
        conn = store.connect(readonly=True)
        try:
            guard_file(caller, ws, _bindings(conn, ws.alias), ws.relative_of(resolved))
        finally:
            conn.close()
        if known is None:
            resolved.path.parent.mkdir(parents=True, exist_ok=True)
            if is_link_or_reparse(resolved.path.parent):
                raise PrometheosError("policy_denied", "links and reparse points are refused", {})
            with open(resolved.path, "xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            written = True
        label = re.sub(r"[^a-z0-9]+", "-", stem)
        caption = _bib_text(source["body"]["title"])
        result |= {
            "snippet": "\\begin{figure}[htbp]\n  \\centering\n"
            f"  \\includegraphics[width=\\linewidth]{{{name}}}\n"
            f"  \\caption{{{caption}}}\n  \\label{{fig:{label}}}\n\\end{{figure}}\n",
            "file": name,
            "written": written,
            "needs_package": "graphicx",
        }
    result["relation"] = _relation(store, version, ws.paper, actor, max_rank)
    return result


def require_paper(conn: sqlite3.Connection, caller: Caller, paper_id: str) -> dict[str, Any]:
    """The paper if the caller may see it; absent and restricted papers answer alike."""
    return _record(conn, caller, paper_id, "paper")
