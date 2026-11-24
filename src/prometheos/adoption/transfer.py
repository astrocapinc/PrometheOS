"""Moving an accepted baseline: an export package and its re-import into a fresh store, and a
one-way fenced Markdown export for a notes vault (A05, CP17.6; C17-INCREMENTAL; decision 0045).

- **Package** (``prometheos-adoption-baseline``, format 1): the baseline, its scan generation
  (scan, inventory rows, evidence) and the owner's decisions for that folder, with a SHA-256 of
  the canonical content. Re-importing it into another store recreates the generation and the
  baseline; fused with the package's own decisions, the families must equal those the baseline
  accepted (members, current members with digests, bands), or nothing is imported. The stored
  families then follow this store's decisions for the folder, if it has any. Importing owner
  decisions is an owner act (an agent could otherwise forge T0), so the import is on owner
  routes only.
- **Markdown export**: a note ``adoption-<root>.md`` in a folder of an approved root, holding
  the baseline's families, current files, decisions and open plan steps between fences
  (``<!-- prometheos:begin adoption ... -->`` ... ``<!-- prometheos:end adoption ... -->``).
  A later export replaces only the fenced block; everything outside it is the owner's and is
  kept byte for byte. It is one-way: PrometheOS never reads the block back as data, and vault
  import skips notes that carry a PrometheOS fence. Writing outside the store is an owner act.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from prometheos.adoption import fusion, plan, tables
from prometheos.adoption.contracts import AdoptionBaselinePackage
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.models import PrometheosError, utc_now

KIND = "prometheos-adoption-baseline"
FORMAT = 1
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
FENCE_BEGIN = "<!-- prometheos:begin adoption"
FENCE_END = "<!-- prometheos:end adoption"


# ----- package -------------------------------------------------------------------------------


def export_package(conn: sqlite3.Connection, baseline_id: str) -> dict[str, Any]:
    baseline = plan.baseline(conn, baseline_id)
    scan = tables.require_scan(conn, baseline["scan_gen"])
    decisions = [
        {
            "family_id": r["family_id"],
            "current_path": r["current_path"],
            "current_sha256": r["current_sha256"],
            "note": r["note"],
            "decided_by": r["decided_by"],
            "decided_at": r["decided_at"],
        }
        for r in conn.execute(
            # only the decisions in force when the baseline was accepted (review cp17-6-a F3)
            "SELECT * FROM adoption_decisions WHERE root_alias = ? AND decided_at <= ? "
            "ORDER BY decided_at, decision_id",
            (scan.root_alias, baseline["accepted_at"]),
        ).fetchall()
    ]
    class_decisions = [
        {k: r[k] for k in ("family_key", "members", "decision", "note", "decided_by",
                           "decided_at")}
        for r in conn.execute(
            # the class decisions in force when the baseline was accepted (decision 0106)
            "SELECT * FROM adoption_class_decisions WHERE root_alias = ? AND decided_at <= ? "
            "ORDER BY decided_at, decision_id",
            (scan.root_alias, baseline["accepted_at"]),
        ).fetchall()
    ]  # fmt: skip
    content = {
        "kind": KIND,
        "format": FORMAT,
        "baseline": {k: baseline[k] for k in ("baseline_id", "sha256", "payload", "accepted_at",
                                               "accepted_by", "plan_id", "plan_version")},
        "scan": scan.model_dump(mode="json"),
        "files": [r.model_dump(mode="json") for r in tables.all_files(conn, scan.scan_gen)],
        "evidence": [e.model_dump(mode="json") for e in tables.all_evidence(conn, scan.scan_gen)],
        "decisions": decisions,
    }  # fmt: skip
    if class_decisions:
        content["class_decisions"] = class_decisions
    package = {**content, "sha256": canonical_sha256(content)}
    if len(dumps_canonical(package)) > MAX_PACKAGE_BYTES:
        raise PrometheosError("budget_exceeded", "the baseline package is over 64 MiB", {})
    return package


def import_package(
    store: Any,
    package: Any,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """Recreate a baseline's generation in this store (a new scan generation number), with
    its decisions, families and baseline. Refused when the digest does not match, the store
    already holds that baseline, or a row does not validate."""
    from prometheos.store import capabilities, db
    from prometheos.store.project import ProjectStore

    if not isinstance(package, dict) or package.get("kind") != KIND:
        raise PrometheosError("validation_error", "not an adoption baseline package", {})
    if package.get("format") != FORMAT:
        raise PrometheosError("validation_error", "unknown package format", {})
    content = {k: v for k, v in package.items() if k != "sha256"}
    if canonical_sha256(content) != package.get("sha256"):
        raise PrometheosError("validation_error", "the package digest does not match", {})
    try:
        parsed = AdoptionBaselinePackage.model_validate(package)
    except ValueError as exc:
        raise PrometheosError("validation_error", "a package row does not validate", {}) from exc
    if canonical_sha256(parsed.baseline.payload) != parsed.baseline.sha256:
        raise PrometheosError("validation_error", "the baseline digest does not match", {})
    old_scan, files, evidence = parsed.scan, parsed.files, parsed.evidence
    baseline = parsed.baseline.model_dump(mode="json")
    decisions = [d.model_dump(mode="json") for d in parsed.decisions]
    class_decisions = [d.model_dump(mode="json") for d in parsed.class_decisions]
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            if grant is not None:
                capabilities.consume(
                    conn,
                    secret=grant,
                    project_id=ProjectStore.project_id_in(conn),
                    operation="exact_request",
                    digest=grant_digest,
                )
            if conn.execute(
                "SELECT 1 FROM adoption_baselines WHERE baseline_id = ?", (baseline["baseline_id"],)
            ).fetchone():
                raise PrometheosError(
                    "validation_error", "this store already holds the baseline", {}
                )
            gen = tables.next_scan_gen(conn)
            scan = old_scan.model_copy(
                update={"scan_gen": gen, "run_id": None, "config": old_scan.config.model_copy(
                    update={"previous_gen": None})}
            )  # fmt: skip
            tables.insert_scan(conn, scan)
            tables.insert_files(conn, [f.model_copy(update={"scan_gen": gen}) for f in files])
            tables.replace_evidence(
                conn, gen, [e.model_copy(update={"scan_gen": gen}) for e in evidence]
            )
            for d in decisions:
                conn.execute(
                    "INSERT INTO adoption_decisions (decision_id, root_alias, family_id, "
                    "current_path, current_sha256, note, decided_by, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("dec_" + canonical_sha256([baseline["baseline_id"], d])[:24], scan.root_alias,
                     d["family_id"], d["current_path"], d["current_sha256"], d["note"],
                     str(d["decided_by"])[:100], d["decided_at"]),
                )  # fmt: skip
            for d in class_decisions:
                conn.execute(
                    "INSERT OR IGNORE INTO adoption_class_decisions (decision_id, root_alias, "
                    "family_key, members, decision, note, decided_by, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("cdec_" + canonical_sha256([baseline["baseline_id"], d])[:24],
                     scan.root_alias, d["family_key"], d["members"], d["decision"], d["note"],
                     str(d["decided_by"])[:100], d["decided_at"]),
                )  # fmt: skip
            families = plan.fuse_again(conn, gen)
            # The package must reproduce the families its baseline accepted: fused with the
            # package's own decisions only (this store's own decisions for the folder may
            # differ and do not make a genuine package wrong; review cp17-6-a-r3 R3-2).
            # Otherwise the import is refused and nothing is kept (review cp17-6-a-r2 R2-2).
            rows = [f.model_copy(update={"scan_gen": gen}) for f in files]
            # A scan made without ranking has no families (decision 0041 section 5), so its
            # baseline has none either (review cp17-6-a-r4 R4-1).
            proof = (
                fusion.fuse(
                    gen,
                    rows,
                    [e.model_copy(update={"scan_gen": gen}) for e in evidence],
                    _package_decisions(decisions, rows),
                )
                if old_scan.config.rank
                else []
            )
            # key, current, band, and every member with its digest as the rows say (review
            # cp17-6-a-r4 R4-3: members and digests are compared too)
            digest = {r.path: r.sha256 for r in rows}
            fused = {
                (
                    f.key,
                    f.current,
                    digest.get(f.current) if f.current else None,
                    f.band,
                    tuple(sorted((m.path, digest.get(m.path)) for m in f.members)),
                )
                for f in proof
            }
            accepted = {
                (f["key"], f["current"], f.get("current_sha256"), f["band"],
                 tuple(sorted((p, s) for p, s in f.get("members", []))))
                for f in baseline["payload"].get("families", [])
            }  # fmt: skip
            if baseline["payload"].get("root_alias") != old_scan.root_alias:
                raise PrometheosError(
                    "validation_error", "the baseline is of another folder than its scan", {}
                )
            if fused != accepted:
                first = sorted(fused ^ accepted, key=str)[0]
                raise PrometheosError(
                    "validation_error",
                    "the package does not reproduce the families its baseline accepted "
                    f"(for example {first[0]}); it was not imported",
                    {"differ": len(fused ^ accepted)},
                )
            conn.execute(
                "INSERT INTO adoption_baselines (baseline_id, scan_gen, plan_id, plan_version, "
                "accepted_at, accepted_by, sha256, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (baseline["baseline_id"], gen, baseline["plan_id"], baseline["plan_version"],
                 baseline["accepted_at"], str(baseline["accepted_by"])[:100], baseline["sha256"],
                 dumps_canonical(baseline["payload"]).decode()),
            )  # fmt: skip
    finally:
        conn.close()
    return {
        "baseline_id": baseline["baseline_id"],
        "scan_gen": gen,
        "root_alias": scan.root_alias,
        "files": len(files),
        "evidence": len(evidence),
        "decisions": len(decisions),
        "families": len(families),
        "imported_by": actor[:100],
        "imported_at": utc_now(),
    }


def _package_decisions(decisions: list[dict[str, Any]], rows: list[Any]) -> dict[str, str]:
    """The package's decisions resolved in its own rows, as ``plan.decisions_for`` does: each
    family's latest, by path and digest or by digest, oldest to newest."""
    by_path = {r.path: r for r in rows}
    by_sha: dict[str, str] = {}
    for r in rows:
        if r.sha256 and r.entry == "file":
            by_sha.setdefault(r.sha256, r.path)
    latest: dict[str, dict[str, Any]] = {}
    for d in sorted(decisions, key=lambda d: d["decided_at"]):
        latest.pop(d["family_id"], None)
        latest[d["family_id"]] = d
    out: dict[str, str] = {}
    for fid, d in latest.items():
        path, sha = d["current_path"], d["current_sha256"]
        if path in by_path and sha in (None, by_path[path].sha256):
            out[fid] = path
        elif sha in by_sha:
            out[fid] = by_sha[sha]
    return out


# ----- one-way fenced Markdown export ----------------------------------------------------------


def _cell(text: Any) -> str:
    value = "" if text is None else str(text)
    return value.replace("|", "\\|").replace("`", "'").replace("\n", " ")[:300]


def markdown_block(conn: sqlite3.Connection, baseline_id: str) -> str:
    baseline = plan.baseline(conn, baseline_id)
    payload = baseline["payload"]
    root = payload["root_alias"]
    decided = payload.get("decisions") or {}
    lines = [
        f"{FENCE_BEGIN} root={root} baseline={baseline_id} sha256={baseline['sha256'][:16]} -->",
        "",
        f"## Adoption baseline of `{_cell(root)}`",
        "",
        f"Accepted {baseline['accepted_at']} (scan {baseline['scan_gen']}, plan version "
        f"{baseline['plan_version']}). Written by PrometheOS; edits inside this block are "
        "replaced by the next export, text outside it is yours.",
        "",
    ]
    scan = tables.require_scan(conn, baseline["scan_gen"])
    for item in scan.analysis.analyzers_unavailable if scan.analysis else []:
        # decision 0053 (review cp17-0053-a F3): the note says what was not analysed
        lines += [
            f"Not analysed by {_cell(item.analyzer)}: {_cell(item.reason)}; {item.files} "
            f"file(s) were inventoried but not parsed ({_cell(item.install)}).",
            "",
        ]
    lines += [
        "| Family | Band | Current file | Members | Decided by you |",
        "|---|---|---|---|---|",
    ]
    for family in sorted(payload.get("families", []), key=lambda f: f["key"]):
        current = f"`{_cell(family['current'])}`" if family.get("current") else "(undecided)"
        lines.append(
            f"| {_cell(family['key'])} | {_cell(family['band'])} | {current} | "
            f"{len(family.get('members', []))} | "
            f"{'yes' if family['family_id'] in decided else ''} |"
        )
    open_steps = [
        row
        for row in plan.step_rows(conn, baseline["plan_id"]).values()
        if row["state"] in ("ready", "validated", "rejected")
    ]
    if open_steps:
        lines += ["", "Open plan steps:", ""]
        for row in sorted(open_steps, key=lambda r: r["step"].step_id):
            lines.append(f"- {_cell(row['step'].title)} ({row['state']})")
    lines += ["", f"{FENCE_END} root={root} -->"]
    return "\n".join(lines) + "\n"


_FENCED = re.compile(
    r"<!-- prometheos:begin adoption root=(?P<root>\S+) .*?-->.*?"
    r"<!-- prometheos:end adoption root=(?P=root) -->(?:\r?\n)?",
    re.DOTALL,
)


def merge(existing: str | None, block: str, root: str) -> str:
    """The note with this root's fenced block replaced (or appended); text outside is kept
    byte for byte, and the block follows the note's own line endings (review cp17-6-a F1)."""
    if not existing:
        return block
    if "\r\n" in existing:
        block = block.replace("\n", "\r\n")
    newline = "\r\n" if "\r\n" in existing else "\n"
    matches = [m for m in _FENCED.finditer(existing) if m.group("root") == root]
    if len(matches) > 1:
        raise PrometheosError(
            "validation_error", "the note holds this folder's block more than once", {}
        )
    if matches:
        m = matches[0]
        return existing[: m.start()] + block + existing[m.end() :]
    if FENCE_BEGIN + f" root={root} " in existing:
        raise PrometheosError(
            "validation_error", "the note's PrometheOS block is damaged; fix or remove it", {}
        )
    return existing + ("" if existing.endswith("\n") else newline) + newline + block


def _attributes(path: Path) -> int:
    """The file's Windows attributes (a cloud placeholder's recall bits included)."""
    return int(getattr(os.stat(path), "st_file_attributes", 0))


def note_name(root_alias: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", root_alias).strip("-") or "folder"
    return f"adoption-{safe}.md"


def write_note(folder: Path, root: Path, name: str, text: str, previous: str | None) -> str:
    """Write the note atomically inside ``folder`` (within the approved ``root``), refusing
    links, junctions and placeholders on the way and a note that changed since it was read."""
    from prometheos.adoption.inventory import PLACEHOLDER_BITS
    from prometheos.sources.paths import is_link_or_reparse

    target = folder / name
    resolved_root = root.resolve()
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.exists() and is_link_or_reparse(current):
            raise PrometheosError("policy_denied", "links or junctions on the way are refused", {})
    if not target.resolve().is_relative_to(resolved_root):
        raise PrometheosError("policy_denied", "the note would leave the approved folder", {})
    if target.exists():
        attributes = _attributes(target)
        if attributes & PLACEHOLDER_BITS:
            raise PrometheosError("policy_denied", "the note is a cloud placeholder", {})
        if _decoded(target.read_bytes()) != (previous or ""):
            raise PrometheosError("revision_conflict", "the note changed; export again", {})
    elif previous is not None:
        raise PrometheosError("revision_conflict", "the note was removed; export again", {})
    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".prometheos-tmp")
    tmp.write_bytes(text.encode("utf-8"))  # no newline translation: outside text kept as is
    os.replace(tmp, target)
    return canonical_sha256({"text": text})


def read_note(folder: Path, name: str) -> str | None:
    target = folder / name
    if not target.exists():
        return None
    from prometheos.adoption.inventory import PLACEHOLDER_BITS

    if _attributes(target) & PLACEHOLDER_BITS:
        raise PrometheosError("policy_denied", "the note is a cloud placeholder", {})
    return _decoded(target.read_bytes())


def _decoded(data: bytes) -> str:
    """A note's exact text (a BOM kept as U+FEFF, line endings untouched); only UTF-8 notes are
    rewritten, anything else is refused as a domain error, never a raw exception."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrometheosError(
            "validation_error", "the note is not UTF-8; PrometheOS writes only UTF-8 notes", {}
        ) from exc
