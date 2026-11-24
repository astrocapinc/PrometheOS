"""Bibliography files in ``library import`` (CP11 S11.2; decisions 0062 §2 and 0063).

A ``.bib``, ``.ris`` or ``.csv`` file under the import scope is kept as a source version (its
original bytes, ``payload_schema`` text) and each of its entries becomes a work or contributes
to one. Exact dedup is by normalised identity identifiers (DOI, arXiv without version, PMID,
PMCID): within one import, across imports and against S11.1's PDF works. A work's fields are
composed from all its provenance records by a fixed precedence; every record is kept:

1. bibliography records beat PDF-derived ones (``pdf_info``, then ``first_page_text``, then
   ``file_name``);
2. among records of one rank, the earlier one wins;
3. a re-imported, changed file replaces only its own records for an entry whose values
   changed, so an unchanged entry is not revised.

An entry whose identifiers point at two different works, or carry two DOIs, or contradict the
matched work's identifiers, is refused for review, never guessed. A preprint and its published
version are linked with ``version_of`` when one entry names the other's identifier.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from prometheos.app import reads, sources
from prometheos.domain.bodies import SourceBody, VersionBody, WorkBody
from prometheos.domain.ids import derived_request_id
from prometheos.domain.models import SENSITIVITY_ORDER, PrometheosError
from prometheos.library import biblio
from prometheos.library import index as library_index
from prometheos.library.identifiers import IDENTITY_SCHEMES
from prometheos.store import derived
from prometheos.store.project import Txn, live

RANK = {"bibliography": 0, "zotero": 0, "pdf_info": 1, "first_page_text": 2, "file_name": 3}
BIB_BASES = ("bibliography", "zotero")
SCALAR_FIELDS = ("title", "year", "venue", "entry_type")
ID_FIELDS = ("doi", "arxiv", "pmid", "pmcid", "isbn")
RANK_NAME = {0: "normal", 1: "restricted", 2: "private"}
EDITOR = " (editor)"


def is_bibliography(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in biblio.FORMATS


def format_of(path: str) -> str:
    return biblio.FORMATS[PurePosixPath(path).suffix.lower()]


def work_id_for(plan_id: str, item: int, entry: int) -> str:
    """The ID a new work of a plan's entry gets: derived, so the plan names it and a resumed or
    replayed commit creates exactly that one."""
    return "work_" + derived_request_id("library-work", plan_id, str(item), str(entry))


# ----- the store, or a plan's simulation of it ------------------------------------------------


@dataclass
class View:
    """What resolution reads: the store (``conn``, the caller's rank) and, while planning, the
    works earlier entries of the same plan would create or change (``planned``)."""

    conn: Any
    max_rank: int
    planned: dict[str, dict[str, Any]] = field(default_factory=dict)  # work -> body
    planned_ids: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    planned_entries: dict[tuple[str, str], str] = field(default_factory=dict)
    planned_versions: dict[str, str] = field(default_factory=dict)  # version -> source
    planned_sha: dict[str, str] = field(default_factory=dict)  # version -> its sha256

    def body(self, work_id: str) -> tuple[dict[str, Any], str, int] | None:
        """``(body, sensitivity, rev)`` of a work the caller may see."""
        if work_id in self.planned:
            return self.planned[work_id], "normal", 0
        record = reads.current_record(self.conn, work_id)
        if record is None or record["kind"] != "work" or record["lifecycle"] == "deleted":
            return None
        if SENSITIVITY_ORDER[record["sensitivity"]] > self.max_rank:
            return None
        return record["body"], record["sensitivity"], int(record["rev"])

    def hidden(self, work_id: str) -> bool:
        record = reads.current_record(self.conn, work_id)
        return record is not None and SENSITIVITY_ORDER[record["sensitivity"]] > self.max_rank

    def final(self, work_id: str) -> str:
        """Follow ``merged_into`` to the work that stands for a merged one."""
        seen = set()
        while work_id not in seen:
            seen.add(work_id)
            found = self.body(work_id)
            target = ((found[0] if found else {}).get("merged_into") or {}).get("id")
            if not target:
                return work_id
            work_id = str(target)
        return work_id

    def by_identifier(self, scheme: str, value: str) -> set[str]:
        if not library_index.schema14(self.conn):  # a store before schema 14 has no index
            return {self.final(w) for w in self.planned_ids.get((scheme, value), set())}
        rows = self.conn.execute(
            "SELECT work_id FROM library_identifiers WHERE scheme = ? AND value = ?",
            (scheme, value),
        ).fetchall()
        found = {str(r[0]) for r in rows} | self.planned_ids.get((scheme, value), set())
        return {self.final(w) for w in found}

    def by_entry(self, source_id: str, key: str) -> str | None:
        if (source_id, key) in self.planned_entries:
            return self.planned_entries[(source_id, key)]
        if not library_index.schema14(self.conn):
            return None
        row = self.conn.execute(
            "SELECT work_id FROM library_entries WHERE source_id = ? AND entry_key = ?",
            (source_id, key),
        ).fetchone()
        return self.final(str(row[0])) if row else None

    def sha_of(self, version_id: str) -> str:
        if version_id in self.planned_sha:
            return self.planned_sha[version_id]
        record = reads.current_record(self.conn, version_id)
        return str((record or {}).get("body", {}).get("sha256", ""))

    def source_of(self, version_id: str) -> str | None:
        if version_id in self.planned_versions:
            return self.planned_versions[version_id]
        record = reads.current_record(self.conn, version_id)
        return str(record["body"].get("source_id")) if record else None

    def remember_work(self, work_id: str, body: dict[str, Any]) -> None:
        self.planned[work_id] = body
        for scheme in IDENTITY_SCHEMES:
            value = (body.get("identifiers") or {}).get(scheme)
            if value:
                self.planned_ids.setdefault((scheme, str(value)), set()).add(work_id)

    def remember(self, work_id: str, body: dict[str, Any], source_id: str, key: str) -> None:
        self.planned[work_id] = body
        for scheme in IDENTITY_SCHEMES:
            value = (body.get("identifiers") or {}).get(scheme)
            if value:
                self.planned_ids.setdefault((scheme, str(value)), set()).add(work_id)
        for other in (body.get("identifiers") or {}).get("other") or []:
            if other["scheme"] in ("version_doi", "version_arxiv"):
                self.planned_ids.setdefault((other["scheme"], other["value"]), set()).add(work_id)
        self.planned_entries[(source_id, key)] = work_id


# ----- composing a work from its provenance -----------------------------------------------------


def _contributors_value(contributors: list[dict[str, str]]) -> str:
    return "; ".join(
        c["name"] + (EDITOR if c.get("role") == "editor" else "") for c in contributors
    )[:2000]


def _contributors_of(value: str) -> list[dict[str, str]]:
    out = []
    for part in value.split("; "):
        name = part.strip()
        if not name:
            continue
        role = "editor" if name.endswith(EDITOR) else "author"
        out.append({"name": name.removesuffix(EDITOR)[:300], "role": role})
    return out[:200]


def current_values(body: dict[str, Any]) -> dict[str, str]:
    """A work's fields in the string form provenance records use."""
    out: dict[str, str] = {}
    for name in SCALAR_FIELDS:
        if body.get(name) is not None:
            out[name] = str(body[name])
    if body.get("contributors"):
        out["contributors"] = _contributors_value(body["contributors"])
    for scheme in ID_FIELDS:
        value = (body.get("identifiers") or {}).get(scheme)
        if value:
            out[f"identifiers.{scheme}"] = str(value)
    if out.get("identifiers.arxiv"):
        version = next(
            (o["value"] for o in (body.get("identifiers") or {}).get("other") or []
             if o["scheme"] == "arxiv_version"),
            "",
        )  # fmt: skip
        out["identifiers.arxiv"] += version
    return out


_VERSIONED = re.compile(r"^(.*?)(v[0-9]{1,3})?$")


def _records_for(
    record: biblio.Record, version_id: str, quoted: Callable[[int, int], str]
) -> list[dict[str, Any]]:
    out = []
    for name, f in sorted(record.fields.items(), key=lambda kv: kv[0]):
        locator: dict[str, Any]
        if f.pointer is not None:  # a Zotero item: a JSON pointer into the retained item
            locator = {"type": "json", "pointer": f.pointer}
        else:
            locator = {"type": "text", "start_line": f.start_line, "end_line": f.end_line}
        if name == "abstract":
            # The abstract is a record like any field: its value is the quote digest of the
            # cited lines (a Zotero abstract: of the cited string), so the work's abstract_ref
            # is chosen by the stable key (R2-F1).
            text = f.value if f.pointer is not None else quoted(f.start_line, f.end_line)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            out.append(
                {
                    "field": "abstract_ref",
                    "version_ref": {"id": version_id, "rev": 1},
                    "locator": locator,
                    "basis": record.basis,
                    "value": digest,
                    "entry": record.key[:200],
                }
            )  # fmt: skip
            continue
        out.append(
            {
                "field": name,
                "version_ref": {"id": version_id, "rev": 1},
                "locator": locator,
                "basis": record.basis,
                "value": f.value[:2000],
                "entry": record.key[:200],
            }
        )
    return out


def compose(
    view: View,
    body: dict[str, Any] | None,
    record: biblio.Record,
    source_id: str,
    version_id: str,
    quoted: Callable[[int, int], str],
) -> dict[str, Any]:
    """The work after ``record`` (from ``version_id`` of ``source_id``) contributes to ``body``
    (``None``: a new work). Returns the body unchanged (the same object) when nothing changes."""
    new_records = _records_for(record, version_id, quoted)
    old = list((body or {}).get("field_provenance") or [])
    mine = [
        r
        for r in old
        if r.get("entry") == record.key
        and r.get("basis") == record.basis
        and view.source_of(str(r["version_ref"]["id"])) == source_id
    ]
    # A Zotero item's records also name the retained item version they cite: a newer version of
    # the item revises the work even when no mapped value changed (decision 0070).
    zotero = record.basis == "zotero"
    asserted = sorted(
        (r["field"], r.get("value"), str(r["version_ref"]["id"]) if zotero else "") for r in mine
    )
    fresh = sorted((r["field"], r["value"], version_id if zotero else "") for r in new_records)
    if body is not None and mine and asserted == fresh:
        return body  # the entry did not change: its earlier records stay
    kept = [r for r in old if r not in mine]
    pdf = [r for r in kept if r.get("basis") not in BIB_BASES]
    bib = sorted(
        [r for r in kept if r.get("basis") in BIB_BASES] + new_records,
        key=lambda r: (
            view.sha_of(str(r["version_ref"]["id"])),
            int((r.get("locator") or {}).get("start_line") or 0),
            str(r.get("entry") or ""),
            r["field"],
        ),
    )
    provenance = pdf + bib
    if len(provenance) > 500:
        raise PrometheosError("budget_exceeded", "a work holds at most 500 provenance records", {})
    out: dict[str, Any] = dict(body) if body is not None else {
        "title": None, "contributors": [], "year": None, "venue": None,
        "identifiers": {"doi": None, "arxiv": None, "pmid": None, "pmcid": None, "isbn": None,
                        "other": []},
        "abstract_ref": None, "access_state": "metadata_only", "full_text_refs": [],
        "merged_into": None,
    }  # fmt: skip
    identifiers = dict(out["identifiers"])
    out["identifiers"] = identifiers

    def best(name: str) -> str | None:
        """The winning value: basis rank, then the citing file's digest, then the line, then
        the entry key and the value itself. Nothing depends on the order of imports."""
        ranked = [
            (
                RANK.get(str(r.get("basis")), 9),
                view.sha_of(str(r["version_ref"]["id"])),
                int((r.get("locator") or {}).get("start_line") or 0),
                str(r.get("entry") or ""),
                str(r.get("value")),
            )
            for r in provenance
            if r["field"] == name and r.get("value") is not None
        ]
        return min(ranked)[4] if ranked else None

    for name in SCALAR_FIELDS:
        value = best(name)
        if value is not None:
            out[name] = int(value) if name == "year" else value
    contributors = best("contributors")
    if contributors is not None:
        out["contributors"] = _contributors_of(contributors)
    arxiv_version: str | None = None
    for scheme in ID_FIELDS:
        value = best(f"identifiers.{scheme}")
        if value is not None and scheme == "arxiv":
            match = _VERSIONED.match(value)
            assert match is not None
            value, arxiv_version = match.group(1), match.group(2)
        if value is not None:
            identifiers[scheme] = value
    other = [dict(o) for o in identifiers.get("other") or []]

    def add_other(scheme: str, value: str) -> None:
        entry = {"scheme": scheme, "value": value[:300]}
        if entry not in other:
            other.append(entry)

    if record.basis == "bibliography" or record.citekey:
        add_other("citekey", record.citekey or record.key)
    if best("identifiers.arxiv") is not None:
        # The version of the winning arXiv record, never the last import's (R2-F1).
        other = [o for o in other if o["scheme"] != "arxiv_version"]
        if arxiv_version:
            add_other("arxiv_version", arxiv_version)
    for scheme, value in sorted(record.related.items()):
        add_other(scheme, value)
    identifiers["other"] = sorted(other, key=lambda o: (o["scheme"], o["value"]))[:100]
    abstracts = [
        r
        for r in provenance
        if r["field"] == "abstract_ref" and r.get("basis") in BIB_BASES and r.get("value")
    ]
    if abstracts:
        chosen = min(
            abstracts,
            key=lambda r: (
                view.sha_of(str(r["version_ref"]["id"])),
                int(r["locator"].get("start_line") or 0),
                str(r.get("entry") or ""),
                str(r["value"]),
            ),
        )
        version = str(chosen["version_ref"]["id"])
        out["abstract_ref"] = {
            "source_id": view.source_of(version) or source_id,
            "version_id": version,
            "locator": dict(chosen["locator"]),
            "quote_sha256": chosen["value"],
        }
    if out["access_state"] != "full_text":
        out["access_state"] = "abstract_only" if out.get("abstract_ref") else "metadata_only"
    out["field_provenance"] = provenance
    if not out.get("title"):
        raise PrometheosError("validation_error", "an entry without a title starts no work", {})
    try:
        return WorkBody.model_validate(out, strict=False).model_dump(mode="json")
    except ValidationError as exc:
        where = "; ".join(
            f"{'/'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3]
        )
        raise PrometheosError("validation_error", f"invalid fields ({where})"[:300], {}) from exc


def pdf_identity(body: dict[str, Any]) -> dict[str, Any]:
    """One identity per PDF work, as for a bibliography entry (review F1): a PDF whose first
    page carries both a DOI and an arXiv identifier is the published work; its arXiv identifier
    names the preprint (``version_arxiv``) and stays in the provenance without a value."""
    ids = dict(body["identifiers"])
    if not (ids.get("doi") and ids.get("arxiv")) or str(ids["doi"]).startswith("10.48550/arxiv."):
        return body
    arxiv = str(ids["arxiv"])
    ids["arxiv"] = None
    other = [o for o in ids.get("other") or [] if o["scheme"] != "version_arxiv"]
    other.append({"scheme": "version_arxiv", "value": arxiv})
    ids["other"] = sorted(other, key=lambda o: (o["scheme"], o["value"]))
    provenance = []
    for record in body["field_provenance"]:
        if record["field"] == "identifiers.arxiv":
            record = {k: v for k, v in record.items() if k != "value"}
        provenance.append(record)
    return {**body, "identifiers": ids, "field_provenance": provenance}


def attach_pdf(existing: dict[str, Any], pdf_body: dict[str, Any]) -> dict[str, Any]:
    """``existing`` with a PDF's full text and records (``pdf_body`` as S11.1 builds it).
    Bibliography fields and records stay (they take precedence); every field no bibliography
    record asserts takes the PDF's value, and the earlier PDF-derived records are replaced."""
    provenance = [r for r in existing.get("field_provenance") or [] if r.get("basis") in BIB_BASES]
    from_bib = {r["field"] for r in provenance}
    out = dict(existing)
    identifiers = dict(existing.get("identifiers") or {})
    for name in ("title", "year", "venue", "contributors"):
        if name not in from_bib:
            out[name] = pdf_body.get(name)
    for scheme in ID_FIELDS:
        if f"identifiers.{scheme}" not in from_bib:
            identifiers[scheme] = (pdf_body.get("identifiers") or {}).get(scheme)
    other = [dict(o) for o in identifiers.get("other") or []]
    for entry in (pdf_body.get("identifiers") or {}).get("other") or []:
        if entry not in other:
            other.append(dict(entry))
    identifiers["other"] = sorted(other, key=lambda o: (o["scheme"], o["value"]))[:100]
    out["identifiers"] = identifiers
    ref = existing.get("abstract_ref")
    if not (ref and ref.get("locator", {}).get("type") in ("text", "json")):
        out["abstract_ref"] = pdf_body.get("abstract_ref")
    out["full_text_refs"] = pdf_body["full_text_refs"]
    out["access_state"] = pdf_body["access_state"]
    out["field_provenance"] = provenance + list(pdf_body.get("field_provenance") or [])
    if not out.get("title"):
        out["title"] = pdf_body["title"]
    return WorkBody.model_validate(out, strict=False).model_dump(mode="json")


# ----- resolving one entry ----------------------------------------------------------------------


@dataclass
class Resolved:
    action: str  # create, merge, update, unchanged, refused
    work_id: str | None = None
    reason: str | None = None
    body: dict[str, Any] | None = None
    base_rev: int = 0
    sensitivity: str | None = None


def resolve(
    view: View,
    record: biblio.Record,
    source_id: str,
    version_id: str,
    new_id: str,
    quoted: Callable[[int, int], str],
) -> Resolved:
    if record.error:
        return Resolved("refused", reason=record.error)
    targets: set[str] = set()
    named = record.work_ref if record.work_ref and view.body(record.work_ref) else None
    if named:
        targets.add(view.final(named))
    previous = view.by_entry(source_id, record.key)
    if previous:
        targets.add(previous)
    for scheme, value in sorted(record.identity.items()):
        targets |= view.by_identifier(scheme, value)
    if any(view.hidden(t) for t in targets):
        return Resolved(
            "refused",
            reason="matches a work outside this scope: importing it needs owner scope",
        )
    if len(targets) > 1:
        return Resolved(
            "refused",
            reason="conflicting identifiers: the entry matches "
            f"{len(targets)} different works; refused for review",
        )
    if not targets:
        try:
            body = compose(view, None, record, source_id, version_id, quoted)
        except PrometheosError as exc:
            return Resolved("refused", reason=exc.message)
        return Resolved("create", new_id, body=body)
    (target,) = targets
    found = view.body(target)
    assert found is not None
    current, sensitivity, rev = found
    have = current.get("identifiers") or {}
    for scheme, value in sorted(record.identity.items()):
        if have.get(scheme) and have[scheme] != value:
            return Resolved(
                "refused",
                reason=f"conflicting identifiers: the matched work has another {scheme}; "
                "refused for review",
            )
    if named and all(
        current_values(current).get(name) == value
        for name, value in biblio.values_of(record).items()
        if name != "entry_type" or current.get("entry_type") is not None
    ):
        return Resolved("unchanged", target, body=current, base_rev=rev, sensitivity=sensitivity)
    try:
        body = compose(view, current, record, source_id, version_id, quoted)
    except PrometheosError as exc:
        return Resolved("refused", reason=exc.message)
    if body is current:
        return Resolved("unchanged", target, body=current, base_rev=rev, sensitivity=sensitivity)
    action = "update" if previous == target else "merge"
    return Resolved(action, target, body=body, base_rev=rev, sensitivity=sensitivity)


def _quoter(text: str) -> Callable[[int, int], str]:
    lines = text.split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    if lines and lines[-1] == "":
        lines.pop()

    def quoted(start: int, end: int) -> str:
        return "\n".join(lines[start - 1 : end])

    return quoted


# ----- planning ---------------------------------------------------------------------------------


def plan_file(
    view: View,
    *,
    plan_id: str,
    index: int,
    alias: str,
    rel: str,
    data: bytes,
    sha: str,
    csv_columns: dict[str, str] | None,
) -> dict[str, Any]:
    """The plan of one bibliography file: what each entry would become. Raises
    :class:`biblio.FileRefused` for a file that cannot be read."""
    fmt = format_of(rel)
    text = biblio.decode(data)
    parsed = biblio.parse(fmt, text, csv_columns)
    existing = sources._source_for_binding(view.conn, alias, rel)
    if existing and SENSITIVITY_ORDER[existing["sensitivity"]] > view.max_rank:
        raise biblio.FileRefused("owner_scope", "bound to a record outside this scope")
    if existing and existing["body"]["retention"] != "copy":
        raise biblio.FileRefused("reference_only", "bound to a reference-only source")
    source_id = (
        existing["id"]
        if existing
        else "source_" + derived_request_id("library-bib-source", plan_id, str(index))
    )
    version_id = "version_" + derived_request_id("library-bib-version", plan_id, str(index))
    item: dict[str, Any] = {
        "kind": "bibliography",
        "format": fmt,
        "source_id": source_id,
        "source_base_rev": int(existing["rev"]) if existing else 0,
        "version_id": version_id,
        "unmapped_columns": parsed.unmapped_columns,
        "column_map": parsed.column_map,
    }
    if existing:
        latest = _latest_sha(view.conn, existing["id"])
        if latest == sha:
            return {**item, "action": "unchanged", "entries": [], "entry_counts": {}}
    copy_of = same_bytes(view.conn, sha, view.max_rank)
    if copy_of is not None:
        # The same bytes are already a bibliography version elsewhere (review F4).
        return {**item, "action": "unchanged", "same_bytes_as": copy_of, "entries": [],
                "entry_counts": {}}  # fmt: skip
    item["action"] = "new_version" if existing else "create"
    view.planned_versions[version_id] = source_id
    view.planned_sha[version_id] = sha
    quoted = _quoter(data.decode("utf-8", "replace"))
    entries = []
    for number, record in enumerate(parsed.records):
        resolved = resolve(
            view, record, source_id, version_id, work_id_for(plan_id, index, number), quoted
        )
        if resolved.action in ("create", "merge", "update") and resolved.body is not None:
            assert resolved.work_id is not None
            view.remember(resolved.work_id, resolved.body, source_id, record.key)
        entries.append(_entry_view(record, resolved))
    item["entries"] = entries
    item["entry_counts"] = _entry_counts(entries)
    if not any(e["action"] in ("create", "merge", "update") for e in entries):
        item["action"] = "unchanged"  # nothing to record: the file itself is not kept either
    return item


def _entry_view(record: biblio.Record, resolved: Resolved) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": record.key[:200],
        "lines": [record.start_line, record.end_line],
        "action": resolved.action,
        "work_id": resolved.work_id,
        "title": (record.fields["title"].value[:300] if "title" in record.fields else None),
        "identifiers": dict(sorted(record.identity.items())),
        "related": dict(sorted(record.related.items())),
        "refused_fields": record.refused_fields[:20],
    }
    if resolved.reason:
        out["reason"] = resolved.reason
    return out


def _entry_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {a: 0 for a in ("create", "merge", "update", "unchanged", "refused")}
    for entry in entries:
        counts[entry["action"]] += 1
    return counts


def same_bytes(conn: Any, sha: str, max_rank: int) -> str | None:
    """The root path of a bibliography source whose version has exactly these bytes and that
    the caller may see, else ``None``."""
    import json

    rows = conn.execute(
        "SELECT r.id, r.sensitivity, b.encoded FROM records r JOIN revisions v ON "
        "v.record_id = r.id AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
        "WHERE r.kind = 'version' AND r.lifecycle != 'deleted' AND b.encoded IS NOT NULL"
    ).fetchall()
    for row in rows:
        body = json.loads(row[2])
        if body.get("sha256") != sha or body.get("mime") not in biblio.MIME.values():
            continue
        if SENSITIVITY_ORDER[str(row[1])] > max_rank:
            continue
        source = reads.current_record(conn, str(body["source_id"]))
        binding = (source or {}).get("body", {}).get("root_binding") or {}
        return str(binding.get("path") or body["source_id"])
    return None


def _latest_sha(conn: Any, source_id: str) -> str | None:
    rows = conn.execute(
        "SELECT b.encoded FROM records r JOIN revisions v ON v.record_id = r.id AND "
        "v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id WHERE r.kind = 'version' "
        "AND b.encoded IS NOT NULL ORDER BY r.created_seq DESC"
    ).fetchall()
    import json

    for row in rows:
        body = json.loads(row[0])
        if body.get("source_id") == source_id:
            return str(body["sha256"])
    return None


# ----- committing -------------------------------------------------------------------------------


class Unchanged(PrometheosError):
    def __init__(self) -> None:
        super().__init__("validation_error", "every entry is already recorded", {})


def write_file(
    txn: Txn,
    *,
    plan: dict[str, Any],
    index: int,
    item: dict[str, Any],
    data: bytes,
) -> dict[str, Any]:
    """All records of one bibliography file in the commit's transaction: the file's source and
    version, then each entry resolved against the store as it is now (entries of this file see
    the works the earlier ones wrote). Raises :class:`Unchanged` when nothing would be written."""
    text = biblio.decode(data)
    parsed = biblio.parse(item["format"], text, plan.get("csv_columns"))
    view = View(txn.conn, txn.max_rank)
    alias, rel = plan["root_alias"], item["path"]
    sensitivity = plan["sensitivity"]
    source_id, version_id = item["source_id"], item["version_id"]
    if item["source_base_rev"] == 0:
        if sources._source_for_binding(txn.conn, alias, rel) is not None:
            raise PrometheosError("revision_conflict", "the file was registered since the plan", {})
    else:
        source = live(txn.get(source_id))
        if source is None or source.rev != item["source_base_rev"]:
            raise PrometheosError("revision_conflict", "the file's source changed", {})
        sensitivity = _higher(sensitivity, source.sensitivity)
    view.planned_versions[version_id] = source_id
    view.planned_sha[version_id] = hashlib.sha256(data).hexdigest()
    quoted = _quoter(data.decode("utf-8", "replace"))
    written: list[str] = []
    entry_counts = {a: 0 for a in ("create", "merge", "update", "unchanged", "refused")}
    pending: list[tuple[biblio.Record, Resolved]] = []
    for number, record in enumerate(parsed.records):
        resolved = resolve(
            view, record, source_id, version_id, work_id_for(plan["plan_id"], index, number),
            quoted,
        )  # fmt: skip
        entry_counts[resolved.action] += 1
        if resolved.action in ("create", "merge", "update"):
            assert resolved.work_id is not None and resolved.body is not None
            view.remember(resolved.work_id, resolved.body, source_id, record.key)
            pending.append((record, resolved))
    if not pending:
        raise Unchanged()
    # The file first (the works' provenance cites its version), then the works in order.
    if item["source_base_rev"] == 0:
        body = SourceBody(
            title=PurePosixPath(rel).name[:300], origin="file", origin_uri=None,
            root_binding={"alias": alias, "path": rel},  # type: ignore[arg-type]
            retention="copy", description="Bibliography added by library import.",
        ).model_dump(mode="json")  # fmt: skip
        _write_record(txn, source_id, "source", 1, body, sensitivity)
    sha = hashlib.sha256(data).hexdigest()
    storage = "body" if len(data) <= sources.INLINE_LIMIT else None
    if storage is None:
        raise PrometheosError("budget_exceeded", "a bibliography file is at most 8 MiB", {})
    txn.conn.execute(
        "INSERT OR IGNORE INTO payloads (sha256, data, byte_length, created_seq) VALUES (?,?,?,?)",
        (sha, data, len(data), txn.seq),
    )
    version = VersionBody(
        source_id=source_id, sha256=sha, byte_length=len(data), mime=biblio.MIME[item["format"]],
        captured_at=txn.recorded_at, storage="body", storage_key=f"payload:{sha}",
        availability="retained", extraction="none", derived_from=None, payload_schema="text",
        transformation=None,
    ).model_dump(mode="json")  # fmt: skip
    _write_record(txn, version_id, "version", 1, version, sensitivity)
    derived.index_text(txn.conn, version_id, "version", "", text)
    works: list[dict[str, Any]] = []
    for _record, resolved in pending:
        assert resolved.work_id is not None and resolved.body is not None
        work_id = resolved.work_id
        if resolved.action == "create":
            rev, level = 1, sensitivity
        else:
            existing = live(txn.get(work_id))
            if existing is None:
                raise PrometheosError("revision_conflict", "a matched work changed", {})
            rev, level = existing.rev + 1, _higher(sensitivity, existing.sensitivity)
        _write_record(txn, work_id, "work", rev, resolved.body, level)
        library_index.index_work(txn.conn, work_id, resolved.body)
        works.append({"id": work_id, "rev": rev, "action": resolved.action})
        written.append(work_id)
    links = link_versions(txn, written)
    return {"works": works, "links": links, "entry_counts": entry_counts}


def link_versions(txn: Txn, work_ids: list[str]) -> list[dict[str, str]]:
    """``version_of`` (preprint -> published) for each written work whose entry named another
    work's identifier, or whose identifier another work names. Never duplicated."""
    links: list[dict[str, str]] = []
    for work_id in work_ids:
        state = live(txn.get(work_id))
        if state is None:
            continue
        ids = state.body.get("identifiers") or {}
        pairs: list[tuple[str, str]] = []
        for other in ids.get("other") or []:
            if other["scheme"] == "version_arxiv":  # I am published; that arXiv work is mine
                pairs += [(w, work_id) for w in _works(txn, "arxiv", other["value"])]
            elif other["scheme"] == "version_doi":  # I am a preprint of that DOI work
                pairs += [(work_id, w) for w in _works(txn, "doi", other["value"])]
        if ids.get("arxiv"):
            pairs += [(work_id, w) for w in _works(txn, "version_arxiv", ids["arxiv"])]
        if ids.get("doi"):
            pairs += [(w, work_id) for w in _works(txn, "version_doi", ids["doi"])]
        for preprint, published in pairs:
            if preprint == published or has_link(txn.conn, preprint, published):
                continue
            if decided(txn.conn, preprint, published):
                continue
            add_link(txn, preprint, published)
            links.append({"preprint": preprint, "published": published})
    return links


def _works(txn: Txn, scheme: str, value: str) -> list[str]:
    rows = txn.conn.execute(
        "SELECT w.work_id FROM library_identifiers i JOIN library_works w ON w.work_id = "
        "i.work_id WHERE i.scheme = ? AND i.value = ? AND w.merged_into IS NULL "
        "ORDER BY w.work_id",
        (scheme, value),
    ).fetchall()
    return [str(r[0]) for r in rows if live(txn.get(str(r[0]))) is not None]


def decided(conn: Any, a: str, b: str) -> bool:
    """Whether the owner decided this pair (kept separate, unlinked, merged...)."""
    x, y = (a, b) if a < b else (b, a)
    row = conn.execute(
        "SELECT 1 FROM library_decisions WHERE work_a = ? AND work_b = ?", (x, y)
    ).fetchone()
    return row is not None


def has_link(conn: Any, a: str, b: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM relations WHERE relation = 'version_of' AND retired_seq IS NULL AND "
        "((source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?))",
        (a, b, b, a),
    ).fetchone()
    return row is not None


def add_link(txn: Txn, preprint: str, published: str) -> None:
    source, target = txn.get(preprint), txn.get(published)
    assert source is not None and target is not None
    txn.conn.execute(
        "INSERT INTO relations (source_id, source_rev, target_id, target_rev, relation, "
        "created_seq) VALUES (?,?,?,?,?,?)",
        (preprint, source.rev, published, target.rev, "version_of", txn.seq),
    )
    txn.audit("relation_add:version_of", preprint, source.rev)


def _higher(a: str, b: str) -> str:
    return RANK_NAME[max(SENSITIVITY_ORDER[a], SENSITIVITY_ORDER[b])]


def _write_record(
    txn: Txn, record_id: str, kind: str, rev: int, body: dict[str, Any], sensitivity: str
) -> None:
    txn.op_index = len(txn.results)
    body_id = txn.insert_body(body)
    txn.write_revision(
        record_id, kind, rev, body_id, sensitivity, "active", None, "library_import", body=body
    )
    txn.result(record_id, rev)
