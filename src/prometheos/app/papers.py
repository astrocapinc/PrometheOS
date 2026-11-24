"""Paper readiness, local submission bundles and handoff plans (spec/05; decision 0007).

``check`` is a pure query: it reads one snapshot and returns what is missing or
needs attention -- unchecked venue requirements, corrected claims still in use,
anchors needing review, unavailable artifacts and open reviewer comments. It never
scores a paper or certifies compliance. ``export`` writes one new local zip bound to
exact manuscript and source versions; ``handoff_plan``/``handoff_apply`` copy the
same files into a folder the researcher chose (for example their own Git or
Overleaf-Git clone) without deleting, overwriting unnamed files, running Git or
contacting any remote.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from prometheos import __version__
from prometheos.app.reads import (
    Caller,
    computed_availability,
    current_record,
    project_seq,
    redact_hidden,
)
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.models import PrometheosError, utc_now
from prometheos.sources.paths import is_link_or_reparse

CORRECTED_STATES = ("superseded", "withdrawn", "contested")
UNSUPPORTED_STATES = ("proposed", "inconclusive")
LIMITATIONS = [
    "No acceptance probability, quality score or compliance verdict is computed.",
    "Claim support reflects recorded assessments within their scopes, not scientific validity.",
    "PDF anchor page numbers are not validated against the PDF.",
    "Requirement rows are only as complete as the imported venue sources they cite.",
]
MAX_CANDIDATES = 5
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
HANDOFF_ARTIFACTS = "prometheos-artifacts"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _finding(code: str, record_ids: list[str], **detail: Any) -> dict[str, Any]:
    return {"code": code, "record_ids": record_ids, "detail": detail}


def _visible(conn: sqlite3.Connection, caller: Caller, record_id: str) -> dict[str, Any] | None:
    record = current_record(conn, record_id)
    if record is None or not caller.may_see(record["sensitivity"]):
        return None
    return record


def _paper(conn: sqlite3.Connection, caller: Caller, paper_id: str) -> dict[str, Any]:
    paper = _visible(conn, caller, paper_id)
    if paper is None or paper["kind"] != "paper" or paper["lifecycle"] == "deleted":
        raise PrometheosError("not_found", "paper not found", {"id": paper_id})
    return paper


def latest_version(
    conn: sqlite3.Connection, caller: Caller, source_id: str
) -> dict[str, Any] | None:
    """The newest version of a source the caller may see (``None`` when there is none)."""
    rows = conn.execute(
        "SELECT r.id FROM records r JOIN revisions v ON v.record_id = r.id "
        "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'version' AND json_extract(b.encoded, '$.source_id') = ? "
        "ORDER BY r.created_seq DESC, r.id DESC",
        (source_id,),
    ).fetchall()
    for row in rows:
        record = _visible(conn, caller, row["id"])
        if record is not None:
            return record
    return None


def _anchors(conn: sqlite3.Connection, paper_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.id FROM records r JOIN revisions v ON v.record_id = r.id "
        "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'anchor' AND r.lifecycle = 'active' "
        "AND json_extract(b.encoded, '$.paper_id') = ? ORDER BY r.id",
        (paper_id,),
    ).fetchall()
    return [a for r in rows if (a := current_record(conn, r["id"])) is not None]


def version_bytes(conn: sqlite3.Connection, store_dir: Path, body: dict[str, Any]) -> bytes | None:
    """Retained bytes of a version, verified against its digest (``None`` otherwise)."""
    data: bytes | None = None
    if body.get("storage") == "body":
        row = conn.execute(
            "SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],)
        ).fetchone()
        data = bytes(row["data"]) if row else None
    elif body.get("storage") == "media":
        try:
            data = (store_dir / "media" / str(body["storage_key"])).read_bytes()
        except OSError:
            data = None
    if data is None or hashlib.sha256(data).hexdigest() != body["sha256"]:
        return None
    return data


def _lines(data: bytes | None) -> list[str] | None:
    from prometheos.app.apply import text_lines

    return text_lines(data) if data is not None else None


def overview(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, paper_id: str
) -> dict[str, Any]:
    """A paper with its anchors and manuscript versions (newest first) for the hub."""
    from prometheos.app.reads import present

    paper = _paper(conn, caller, paper_id)
    anchors = [
        present(conn, store_dir, a)
        for a in _anchors(conn, paper_id)
        if caller.may_see(a["sensitivity"])
    ]
    versions: list[dict[str, Any]] = []
    source_id = paper["body"]["manuscript_source_id"]
    if source_id is not None:
        rows = conn.execute(
            "SELECT r.id FROM records r JOIN revisions v ON v.record_id = r.id "
            "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
            "WHERE r.kind = 'version' AND json_extract(b.encoded, '$.source_id') = ? "
            "ORDER BY r.created_seq DESC, r.id DESC LIMIT 20",
            (source_id,),
        ).fetchall()
        for row in rows:
            version = _visible(conn, caller, row["id"])
            if version is not None:
                versions.append(present(conn, store_dir, version))
    used: list[dict[str, Any]] = []
    for vid in sorted(_used_in(conn, caller, paper_id)):
        version = _visible(conn, caller, vid)
        if version is not None and version["kind"] == "version":
            used.append(present(conn, store_dir, version))
    return {
        "project_seq": project_seq(conn),
        "paper": paper,
        "anchors": anchors,
        "manuscript_versions": versions,
        "used_versions": used,
    }


def _used_in(conn: sqlite3.Connection, caller: Caller, paper_id: str) -> set[str]:
    """Versions related ``used_in`` to the paper or its anchors (a source means its latest
    visible version)."""
    targets = [paper_id, *[a["id"] for a in _anchors(conn, paper_id)]]
    marks = ",".join("?" for _ in targets)
    found: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT source_id FROM relations WHERE retired_seq IS NULL "  # noqa: S608
        f"AND relation = 'used_in' AND target_id IN ({marks})",
        targets,
    ):
        if row["source_id"].startswith("version_"):
            found.add(row["source_id"])
        elif row["source_id"].startswith("source_"):
            latest = latest_version(conn, caller, row["source_id"])
            # A source with no visible version stays in the set so a bundle counts it as
            # withheld; callers skip IDs that are not visible versions.
            found.add(latest["id"] if latest is not None else row["source_id"])
    return found


# ----- check -------------------------------------------------------------------------------


def retained_media(
    conn: sqlite3.Connection, caller: Caller, paper_id: str
) -> list[tuple[str, str, int]]:
    """``(storage_key, sha256, byte_length)`` of the media-retained versions :func:`check`
    verifies for this paper: the latest manuscript version, the venue sources, and the
    evidence of its requirements, its claims and its anchors (and their claims). A scheduled
    check hashes them first with no store connection open, so its snapshot then finds them in
    the verification cache (decision 0027). Missing one only costs hashing in the snapshot."""
    body = _paper(conn, caller, paper_id)["body"]
    wanted: set[str] = {ref["id"] for ref in body["venue_source_refs"]}
    if body["manuscript_source_id"] is not None:
        latest = latest_version(conn, caller, body["manuscript_source_id"])
        if latest is not None:
            wanted.add(latest["id"])
    claims: list[str] = [ref["id"] for ref in body["claim_refs"]]
    for row in body["requirements"]:
        wanted.update(ev["version_id"] for ev in row["evidence"])
    for anchor in _anchors(conn, paper_id):
        if caller.may_see(anchor["sensitivity"]):
            wanted.update(ev["version_id"] for ev in anchor["body"]["evidence"])
            claims += [ref["id"] for ref in anchor["body"]["claim_refs"]]
    for claim_id in claims:
        claim = _visible(conn, caller, claim_id)
        if claim is not None:
            wanted.update(ev["version_id"] for ev in claim["body"].get("evidence", []))
    media = []
    for version_id in sorted(wanted):
        version = _visible(conn, caller, version_id)
        if version is not None and version["kind"] == "version":
            vbody = version["body"]
            if vbody.get("storage") == "media":
                media.append(
                    (str(vbody["storage_key"]), vbody["sha256"], int(vbody["byte_length"]))
                )
    return media


def check(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, paper_id: str
) -> dict[str, Any]:
    """Pure readiness analysis of one paper (spec/05 ``paper check``)."""
    paper = _paper(conn, caller, paper_id)
    body = paper["body"]
    findings: list[dict[str, Any]] = []
    hidden = 0
    availability_cache: dict[str, str] = {}
    derived: dict[str, dict[str, Any]] | None = None

    def status_of(claim_id: str) -> dict[str, Any] | None:
        # A06.1 (CP20.1, C20-STATUS): readiness reads the derived status, computed once.
        nonlocal derived
        if derived is None:
            from prometheos.app import claim_status

            derived = {s["claim_id"]: s for s in claim_status.statuses(conn, store_dir, caller)}
        return derived.get(claim_id)

    def availability(version: dict[str, Any]) -> str:
        if version["id"] not in availability_cache:
            availability_cache[version["id"]] = computed_availability(
                conn, store_dir, version["body"], version["id"]
            )
        return availability_cache[version["id"]]

    # Manuscript.
    manuscript: dict[str, Any] = {"source_id": body["manuscript_source_id"], "latest": None}
    latest = None
    if body["manuscript_source_id"] is None:
        findings.append(_finding("manuscript_unavailable", [paper_id], reason="no manuscript"))
    else:
        latest = latest_version(conn, caller, body["manuscript_source_id"])
        if latest is None:
            findings.append(
                _finding(
                    "manuscript_unavailable",
                    [paper_id],
                    reason="no visible manuscript version",
                )
            )
        else:
            state = availability(latest)
            manuscript["latest"] = {
                "version_id": latest["id"],
                "sha256": latest["body"]["sha256"],
                "mime": latest["body"]["mime"],
                "captured_at": latest["body"]["captured_at"],
                "availability": state,
            }
            if state != "retained":
                findings.append(
                    _finding(
                        "manuscript_unavailable", [latest["id"]], reason=f"availability {state}"
                    )
                )

    # Venue and requirements.
    venue_sources = []
    for ref in body["venue_source_refs"]:
        version = _visible(conn, caller, ref["id"])
        if version is None:
            hidden += 1
            continue
        venue_sources.append(
            {
                "version_id": ref["id"],
                "source_id": version["body"]["source_id"],
                "captured_at": version["body"]["captured_at"],
                "availability": availability(version),
            }
        )
    if not body["venue_source_refs"]:
        findings.append(_finding("venue_unknown", [paper_id]))
    for row in body["requirements"]:
        if row["applicability"] == "unknown" or row["status"] in ("unknown", "todo"):
            findings.append(
                _finding(
                    "requirement_unchecked",
                    [paper_id],
                    requirement=row["id"],
                    applicability=row["applicability"],
                    status=row["status"],
                    last_checked_at=row["last_checked_at"],
                )
            )

    # Claims used by the paper and its anchors.
    evidence_refs: list[tuple[str, dict[str, Any]]] = []

    def claim_findings(claim_id: str, users: list[str]) -> None:
        nonlocal hidden
        claim = _visible(conn, caller, claim_id)
        if claim is None:
            hidden += 1
            return
        state = claim["body"].get("state")
        if state in CORRECTED_STATES:
            superseded_by = claim["body"].get("superseded_by")
            findings.append(
                _finding(
                    "claim_corrected_in_use",
                    [*users, claim_id],
                    state=state,
                    superseded_by=superseded_by["id"] if superseded_by else None,
                )
            )
        elif state in UNSUPPORTED_STATES:
            findings.append(_finding("claim_without_support", [*users, claim_id], state=state))
        derived_status = status_of(claim_id)
        if derived_status is not None and state not in CORRECTED_STATES:
            if not derived_status["usable"]:
                # Stale (or superseded or withdrawn by derivation): never offered as usable.
                first = derived_status["causes"][0] if derived_status["causes"] else {}
                findings.append(
                    _finding(
                        "claim_not_usable",
                        [*users, claim_id],
                        status=derived_status["status"],
                        cause=first.get("code"),
                    )
                )
            elif derived_status["basis"] == "agent_reported":
                # Support an agent reported: listed for the owner to confirm.
                findings.append(
                    _finding("claim_agent_reported", [*users, claim_id],
                             status=derived_status["status"])
                )  # fmt: skip
        for ev in claim["body"].get("evidence", []):
            evidence_refs.append((claim_id, ev))

    for ref in body["claim_refs"]:
        claim_findings(ref["id"], [paper_id])

    anchors_out = []
    for anchor in _anchors(conn, paper_id):
        if not caller.may_see(anchor["sensitivity"]):
            hidden += 1
            continue
        a_body = anchor["body"]
        anchors_out.append(
            {
                "id": anchor["id"],
                "rev": anchor["rev"],
                "status": a_body["status"],
                "manuscript_version_id": a_body["manuscript_version_id"],
                "locator": a_body["locator"],
            }
        )
        stale = latest is not None and a_body["manuscript_version_id"] != latest["id"]
        if a_body["status"] == "needs_review":
            findings.append(
                _finding(
                    "anchor_needs_review",
                    [anchor["id"]],
                    reason=a_body["reason"],
                    remap_candidates=_remap_candidates(conn, store_dir, caller, a_body, latest)
                    if stale
                    else [],
                )
            )
        if stale and a_body["status"] != "resolved":
            findings.append(
                _finding(
                    "anchor_stale_version",
                    [anchor["id"]],
                    bound_version=a_body["manuscript_version_id"],
                    latest_version=latest["id"] if latest else None,
                )
            )
        if not a_body["claim_refs"] and not a_body["evidence"]:
            findings.append(_finding("anchor_without_grounding", [anchor["id"]]))
        # Every anchor's claims, resolved ones included: a resolved anchor that still names
        # a corrected claim is reported rather than trusted.
        for ref in a_body["claim_refs"]:
            claim_findings(ref["id"], [anchor["id"]])
        for ev in a_body["evidence"]:
            evidence_refs.append((anchor["id"], ev))
    for row in body["requirements"]:
        for ev in row["evidence"]:
            evidence_refs.append((paper_id, ev))

    # Artifacts behind the evidence.
    seen: set[tuple[str, str]] = set()
    for user, ev in evidence_refs:
        key = (user, ev["version_id"])
        if key in seen:
            continue
        seen.add(key)
        version = _visible(conn, caller, ev["version_id"])
        if version is None:
            hidden += 1
            continue
        state = availability(version)
        if state == "missing":
            findings.append(_finding("artifact_unavailable", [user, ev["version_id"]]))
        elif state == "external":
            findings.append(_finding("artifact_reference_only", [user, ev["version_id"]]))

    # Reviewer comments.
    for review_round in body["review_rounds"]:
        for item in review_round["items"]:
            if item["status"] != "resolved":
                findings.append(
                    _finding(
                        "review_item_open",
                        [paper_id],
                        round=review_round["id"],
                        item=item["id"],
                        status=item["status"],
                    )
                )

    findings = _dedupe(findings)
    findings.sort(key=lambda f: (f["code"], f["record_ids"], dumps_canonical(f["detail"])))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["code"]] = counts.get(f["code"], 0) + 1
    return {
        "paper_id": paper_id,
        "paper_rev": paper["rev"],
        "project_seq": project_seq(conn),
        "title": body["title"],
        "status": body["status"],
        "deadline": body["deadline"],
        "manuscript": manuscript,
        "venue": {"known": bool(body["venue_source_refs"]), "sources": venue_sources},
        "anchors": anchors_out,
        "findings": findings,
        "counts": counts,
        "hidden_references": hidden,
        "findings_digest": canonical_sha256(findings),
        "limitations": LIMITATIONS,
    }


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for f in findings:
        key = dumps_canonical(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _remap_candidates(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    anchor: dict[str, Any],
    latest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Exact candidate positions of the anchor's first line in the latest version."""
    locator = anchor["locator"]
    if latest is None or locator["type"] != "text":
        return []
    old = _visible(conn, caller, anchor["manuscript_version_id"])
    if old is None:
        return []
    old_lines = _lines(version_bytes(conn, store_dir, old["body"]))
    new_lines = _lines(version_bytes(conn, store_dir, latest["body"]))
    if not old_lines or not new_lines or locator["start_line"] > len(old_lines):
        return []
    first = old_lines[locator["start_line"] - 1]
    if not first.strip():
        return []
    width = locator["end_line"] - locator["start_line"]
    out = []
    for number, line in enumerate(new_lines, start=1):
        if line == first:
            out.append(
                {
                    "version_id": latest["id"],
                    "start_line": number,
                    "end_line": min(number + width, len(new_lines)),
                }
            )
        if len(out) == MAX_CANDIDATES:
            break
    return out


def report_entry_text(report: dict[str, Any]) -> str:
    """Saved report text: finding codes and record IDs only (no counts, no record text)."""
    lines = [
        "Deterministic paper readiness check (pure analysis; not a compliance verdict).",
        f"Paper {report['paper_id']}.",
    ]
    if not report["findings"]:
        lines.append("- no findings")
    # Requirement and review-item slugs may contain digits, which generated text may not
    # state unbound; the saved entry names codes and records, the paper holds the rows.
    for f in report["findings"]:
        lines.append(f"- {f['code']}: " + " ".join(f["record_ids"]))
    return "\n".join(lines)


# ----- bundle -------------------------------------------------------------------------------


def _safe_name(name: str, fallback: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._")[:120]
    return cleaned or fallback


def _file_name(conn: sqlite3.Connection, version: dict[str, Any]) -> str:
    source = current_record(conn, version["body"]["source_id"])
    binding = (source or {}).get("body", {}).get("root_binding") if source else None
    if binding:
        return _safe_name(binding["path"].rsplit("/", 1)[-1], version["id"])
    title = (source or {}).get("body", {}).get("title", "") if source else ""
    return _safe_name(title, version["id"])


def _bundle(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    version_id: str | None,
) -> dict[str, Any]:
    """Select the exact bytes of a local bundle; nothing is written here."""
    report = check(conn, store_dir, caller, paper_id)
    paper = _paper(conn, caller, paper_id)
    body = paper["body"]
    if body["manuscript_source_id"] is None:
        raise PrometheosError("validation_error", "the paper has no manuscript source", {})
    if version_id is None:
        manuscript = latest_version(conn, caller, body["manuscript_source_id"])
    else:
        manuscript = _visible(conn, caller, version_id)
        if (
            manuscript is None
            or manuscript["kind"] != "version"
            or manuscript["body"]["source_id"] != body["manuscript_source_id"]
        ):
            raise PrometheosError(
                "not_found", "manuscript version not found for this paper", {"id": version_id}
            )
    if manuscript is None:
        raise PrometheosError("source_unavailable", "no manuscript version to export", {})
    data = version_bytes(conn, store_dir, manuscript["body"])
    if data is None:
        raise PrometheosError(
            "source_unavailable",
            "the manuscript version is not retained intact in this store",
            {"id": manuscript["id"]},
        )
    files = [
        {
            "path": f"manuscript/{_file_name(conn, manuscript)}",
            "role": "manuscript",
            "source_id": manuscript["body"]["source_id"],
            "version_id": manuscript["id"],
            "sha256": manuscript["body"]["sha256"],
            "byte_length": manuscript["body"]["byte_length"],
            "data": data,
        }
    ]
    wanted: dict[str, str] = dict.fromkeys(sorted(_used_in(conn, caller, paper_id)), "used_in")
    for anchor in _anchors(conn, paper_id):
        if caller.may_see(anchor["sensitivity"]):
            for ev in anchor["body"]["evidence"]:
                wanted.setdefault(ev["version_id"], "anchor_evidence")
    for ref in body["claim_refs"]:
        claim = _visible(conn, caller, ref["id"])
        if claim is not None:
            for ev in claim["body"].get("evidence", []):
                wanted.setdefault(ev["version_id"], "claim_evidence")
    wanted.pop(manuscript["id"], None)
    not_included: list[dict[str, Any]] = []
    withheld = 0
    names: set[str] = set()
    for vid in sorted(wanted):
        version = _visible(conn, caller, vid)
        if version is None or version["kind"] != "version":
            withheld += 1
            continue
        blob = version_bytes(conn, store_dir, version["body"])
        if blob is None:
            state = computed_availability(conn, store_dir, version["body"], vid)
            not_included.append(
                {
                    "version_id": vid,
                    "source_id": version["body"]["source_id"],
                    "reason": "reference_only" if state == "external" else "missing",
                }
            )
            continue
        name = _file_name(conn, version)
        if name.lower() in names:
            name = f"{vid}-{name}"
        names.add(name.lower())
        files.append(
            {
                "path": f"artifacts/{name}",
                "role": wanted[vid],
                "source_id": version["body"]["source_id"],
                "version_id": vid,
                "sha256": version["body"]["sha256"],
                "byte_length": version["body"]["byte_length"],
                "data": blob,
            }
        )
    return {
        "report": report,
        "paper": paper,
        "files": files,
        "not_included": not_included,
        "withheld": withheld,
    }


def _manifest(bundle: dict[str, Any], generated_at: str | None) -> dict[str, Any]:
    report = bundle["report"]
    return {
        "schema_version": 1,
        "kind": "prometheos_local_paper_bundle",
        "paper_id": report["paper_id"],
        "paper_rev": report["paper_rev"],
        "paper_title": report["title"],
        "paper_status": report["status"],
        "project_seq": report["project_seq"],
        "generated_at": generated_at,
        "generator": f"prometheos {__version__}",
        "files": [{k: v for k, v in f.items() if k != "data"} for f in bundle["files"]],
        "not_included": bundle["not_included"],
        "withheld_by_sensitivity": bundle["withheld"],
        "check": {
            "findings_digest": report["findings_digest"],
            "counts": report["counts"],
            "findings": report["findings"],
        },
        "statements": [
            "Local bundle only: nothing was submitted, uploaded or sent.",
            "Nothing was compiled; compile outputs appear only if they were registered sources.",
            "Readiness findings are deterministic checks, not a compliance or quality verdict.",
        ],
    }


def export(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    out: Path,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Write one new zip; refuse an existing path. Returns the manifest summary."""
    if out.exists():
        raise PrometheosError("validation_error", "output file already exists", {})
    if not out.parent.is_dir():
        raise PrometheosError("validation_error", "output folder does not exist", {})
    bundle = _bundle(conn, store_dir, caller, paper_id, version_id)
    # Agent scope: references to hidden records never leave in the manifest either.
    manifest = redact_hidden(conn, caller, _manifest(bundle, utc_now()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for f in bundle["files"]:
            if hashlib.sha256(f["data"]).hexdigest() != f["sha256"]:
                raise PrometheosError("io_error", "bytes changed while exporting", {})
            archive.writestr(zipfile.ZipInfo(f["path"], ZIP_TIME), f["data"])
        archive.writestr(
            zipfile.ZipInfo("manifest.json", ZIP_TIME),
            dumps_canonical(manifest) + b"\n",
        )
    payload = buffer.getvalue()
    with open(out, "xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "written": str(out),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "paper_id": paper_id,
        "paper_rev": manifest["paper_rev"],
        "project_seq": manifest["project_seq"],
        "files": manifest["files"],
        "not_included": manifest["not_included"],
        "withheld_by_sensitivity": manifest["withheld_by_sensitivity"],
        "findings_digest": manifest["check"]["findings_digest"],
    }


# ----- handoff ------------------------------------------------------------------------------


def _target_folder(into: Path) -> Path:
    if not into.is_absolute():
        into = into.resolve()
    if not into.is_dir():
        raise PrometheosError("validation_error", "handoff folder does not exist", {})
    if is_link_or_reparse(into):
        raise PrometheosError("policy_denied", "handoff folder is a link or reparse point", {})
    return into


def _existing_files(into: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for current, dirs, names in os.walk(into):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d != ".git" and not is_link_or_reparse(here / d))
        for name in sorted(names):
            path = here / name
            if not is_link_or_reparse(path):
                found[path.relative_to(into).as_posix()] = path
        if len(found) > 5000:
            raise PrometheosError("budget_exceeded", "handoff folder has too many files", {})
    return found


def handoff_plan(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    into: Path,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Pure comparison of the bundle with a local folder (nothing is written)."""
    return _plan(conn, store_dir, caller, paper_id, _target_folder(into), version_id)[0]


def _plan(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    into: Path,
    version_id: str | None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    bundle = _bundle(conn, store_dir, caller, paper_id, version_id)
    contents: dict[str, bytes] = {}
    entries = []
    for f in bundle["files"]:
        name = f["path"].split("/", 1)[1]
        path = name if f["role"] == "manuscript" else f"{HANDOFF_ARTIFACTS}/{name}"
        contents[path] = f["data"]
        entries.append({k: v for k, v in f.items() if k != "data"} | {"path": path})
    manifest_name = f"prometheos-handoff-{paper_id.split('_', 1)[1][:8]}.json"
    manifest = (
        dumps_canonical(
            {
                "kind": "prometheos_handoff",
                "paper_id": paper_id,
                "paper_rev": bundle["report"]["paper_rev"],
                "project_seq": bundle["report"]["project_seq"],
                "files": entries,
            }
        )
        + b"\n"
    )
    contents[manifest_name] = manifest
    entries.append(
        {
            "path": manifest_name,
            "role": "handoff_manifest",
            "source_id": None,
            "version_id": None,
            "sha256": hashlib.sha256(manifest).hexdigest(),
            "byte_length": len(manifest),
        }
    )
    existing = _existing_files(into)
    lowered = {k.lower(): k for k in existing}
    for entry in entries:
        match = lowered.get(entry["path"].lower())
        if match is None:
            entry["action"] = "new"
            entry["existing_sha256"] = None
        else:
            current = hashlib.sha256(existing[match].read_bytes()).hexdigest()
            entry["existing_sha256"] = current
            entry["action"] = "identical" if current == entry["sha256"] else "conflict"
    planned = {e["path"].lower() for e in entries}
    target_only = sorted(k for k in existing if k.lower() not in planned)
    plan = {
        "paper_id": paper_id,
        "paper_rev": bundle["report"]["paper_rev"],
        "project_seq": bundle["report"]["project_seq"],
        "into": str(into),
        "entries": entries,
        "target_only_preserved": target_only[:500],
        "target_only_count": len(target_only),
        "not_included": bundle["not_included"],
        "withheld_by_sensitivity": bundle["withheld"],
        "limits": [
            "Only new files are written; a conflicting file is overwritten only when named.",
            "Files already in the folder and not in the plan are never deleted or changed.",
            "No Git command is run and no remote (Overleaf or other) is contacted.",
            "Bring edits back with source import of the manuscript file.",
        ],
    }
    plan["digest"] = canonical_sha256({k: v for k, v in plan.items() if k != "limits"})
    return plan, contents


def handoff_apply(
    conn: sqlite3.Connection,
    store_dir: Path,
    caller: Caller,
    paper_id: str,
    into: Path,
    digest: str,
    overwrite: list[str],
    version_id: str | None = None,
) -> dict[str, Any]:
    folder = _target_folder(into)
    plan, contents = _plan(conn, store_dir, caller, paper_id, folder, version_id)
    if plan["digest"] != digest:
        raise PrometheosError(
            "revision_conflict",
            "the handoff plan changed (record, bundle or folder); review a new plan",
            {"digest": plan["digest"]},
        )
    conflicts = [e["path"] for e in plan["entries"] if e["action"] == "conflict"]
    named = {p.replace("\\", "/") for p in overwrite}
    unknown = sorted(named - set(conflicts))
    if unknown:
        raise PrometheosError(
            "validation_error", "--overwrite names files that are not conflicts", {"paths": unknown}
        )
    blocked = sorted(set(conflicts) - named)
    if blocked:
        raise PrometheosError(
            "policy_denied",
            "conflicting files would be overwritten; name each one to overwrite it",
            {"conflicts": blocked},
        )
    pending = [e for e in plan["entries"] if e["action"] != "identical"]
    # Preflight every target before writing anything, so a refusal leaves the folder as is.
    for entry in pending:
        target = folder / entry["path"]
        parent = target.parent
        if parent != folder and (
            is_link_or_reparse(parent) or (parent.exists() and not parent.is_dir())
        ):
            raise PrometheosError(
                "policy_denied", "a handoff subfolder is a link or a file", {"path": entry["path"]}
            )
        if is_link_or_reparse(target) or (target.exists() and not target.is_file()):
            raise PrometheosError(
                "policy_denied", "a handoff target is a link or a folder", {"path": entry["path"]}
            )
    written = []
    for entry in pending:
        target = folder / entry["path"]
        parent = target.parent
        if parent != folder:
            parent.mkdir(exist_ok=True)
            if is_link_or_reparse(parent):
                raise PrometheosError("policy_denied", "handoff subfolder is a link", {})
        # A fresh, exclusively created temporary name: never an existing file or link.
        handle_fd, temp_name = tempfile.mkstemp(prefix=".prometheos-handoff-", dir=parent)
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(contents[entry["path"]])
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        written.append({"path": entry["path"], "action": entry["action"]})
    return {
        "into": str(folder),
        "digest": digest,
        "written": written,
        "unchanged": [e["path"] for e in plan["entries"] if e["action"] == "identical"],
        "target_only_preserved_count": plan["target_only_count"],
    }


# ----- venue requirement drafts ----------------------------------------------------------

_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?(?P<text>\S.*)$")


def requirements_draft(
    conn: sqlite3.Connection, store_dir: Path, caller: Caller, version_id: str
) -> dict[str, Any]:
    """List items of a retained text venue artifact as unknown requirement rows (pure)."""
    version = _visible(conn, caller, version_id)
    if version is None or version["kind"] != "version":
        raise PrometheosError("not_found", "version not found", {"id": version_id})
    lines = _lines(version_bytes(conn, store_dir, version["body"]))
    if lines is None:
        raise PrometheosError(
            "source_unavailable", "requirement drafts need retained UTF-8 text", {"id": version_id}
        )
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        match = _LIST_ITEM.match(line)
        if not match:
            continue
        rows.append(
            {
                "id": f"r{len(rows) + 1}",
                "text": match.group("text").strip()[:2000],
                "source_ref": {
                    "source_id": version["body"]["source_id"],
                    "version_id": version_id,
                    "locator": {"type": "text", "start_line": number, "end_line": number},
                    "quote_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                },
                "applicability": "unknown",
                "last_checked_at": None,
                "status": "unknown",
                "evidence": [],
            }
        )
        if len(rows) == 200:
            break
    return {
        "version_id": version_id,
        "source_id": version["body"]["source_id"],
        "captured_at": version["body"]["captured_at"],
        "venue_source_ref": {"id": version_id, "rev": version["rev"]},
        "requirements": rows,
        "note": (
            "Draft rows copied from list items of this exact venue source version. Every row is "
            "unknown until someone records its applicability and status; add them with a paper "
            "revise that also lists this version in venue_source_refs."
        ),
    }
