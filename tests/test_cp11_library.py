"""CP11 S11.1: a PDF folder becomes a located, searchable library (decisions 0060, 0061).

Synthetic PDFs only (``tools/synthetic_pdf.py``): plan and commit through the service, the
records and provenance they create, idempotency by bytes, page-level search with exact spans,
bounded located reads, quotation checks, PDF locators validated by ``check_evidence`` (with
mutation spot-checks of the offset/digest invariant), the hostile set, isolation of the
extraction process, visibility, no write into the folder, migration 13 and derived rebuild and
purge.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import synthetic_pdf as pdfs
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry

from prometheos.adoption.inventory import root_digest
from prometheos.domain.models import PrometheosError
from prometheos.library import contracts, extract
from prometheos.library import index as library_index
from prometheos.library.contracts import PdfPagesV1
from prometheos.store.project import ProjectStore
from prometheos.store.schema import MIGRATIONS

SMALL = {
    "drag/flywheel-drag.pdf": pdfs.paper(pdfs.DRAG),
    "kers-review.pdf": pdfs.paper(pdfs.KERS),
    "scan-without-text.pdf": pdfs.scanned(),
    "duplicates/flywheel-drag-copy.pdf": pdfs.paper(pdfs.DRAG),
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_digest(folder: Path) -> str:
    """Every file's path and bytes under ``folder`` (the walk digest adds stat facts)."""
    h = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        h.update(path.relative_to(folder).as_posix().encode())
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


class Lib:
    def __init__(self, tmp: Path, files: dict[str, bytes] | None = None) -> None:
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.folder = self.env.research / "library"
        pdfs.write_folder(self.folder, SMALL if files is None else files)

    def plan(self, caller: Any = OWNER, **kw: Any) -> dict[str, Any]:
        kw.setdefault("path", "library")
        return dict(self.svc.library_import_plan(self.pid, caller, root_alias="research", **kw))

    def commit(self, plan: dict[str, Any], caller: Any = OWNER) -> dict[str, Any]:
        return dict(self.svc.library_import_commit(self.pid, plan["plan_id"], caller))

    def imported(self) -> dict[str, Any]:
        return self.commit(self.plan())

    def work(self, title_start: str, caller: Any = AGENT) -> dict[str, Any]:
        works = self.svc.library_works(self.pid, caller)["works"]
        return next(w for w in works if w["title"].startswith(title_start))

    def record(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None))

    def seq(self) -> int:
        return int(self.env.seq())

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.env.store_dir / "record.sqlite")
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def lib(tmp_path: Path) -> Lib:
    return Lib(tmp_path)


# ----- plan and commit ----------------------------------------------------------------------


def test_plan_lists_each_pdf_with_digest_pages_and_identifiers(lib: Lib) -> None:
    plan = lib.plan()
    items = {i["path"]: i for i in plan["items"]}
    drag = items["library/drag/flywheel-drag.pdf"]
    data = SMALL["drag/flywheel-drag.pdf"]
    assert drag["action"] == "create"
    assert drag["sha256"] == hashlib.sha256(data).hexdigest()
    assert drag["byte_length"] == len(data)
    assert drag["page_count"] == 4 and drag["pages_without_text"] == [3]
    assert drag["doi"] == "10.5555/synthetic.2026.001" and drag["arxiv"] == "2601.01234"
    assert drag["title_basis"] == "pdf_info"
    # Same bytes under another name: listed once as a duplicate of the first (sorted) path.
    copy = items["library/duplicates/flywheel-drag-copy.pdf"]
    assert copy["action"] == "duplicate" and copy["duplicate_of"] == drag["path"]
    assert items["library/scan-without-text.pdf"]["pages_without_text"] == [1, 2]
    assert {k: v for k, v in plan["counts"].items() if not k.startswith("entries_")} == {
        "create": 3, "new_version": 0, "extract": 0, "unchanged": 0, "duplicate": 1, "refused": 0
    }  # fmt: skip
    assert plan["run_id"].startswith("oprun_")
    assert plan["content_trust"] == "untrusted_data"


def test_commit_creates_source_versions_and_a_work_with_provenance(lib: Lib) -> None:
    result = lib.imported()
    assert result["counts"]["committed"] == 3 and result["counts"]["duplicate"] == 1
    drag = lib.work("Aerodynamic")
    assert drag["page_count"] == 4 and drag["pages_without_text"] == 1
    assert drag["file"] == {"alias": "research", "path": "library/drag/flywheel-drag.pdf"}
    pdf = lib.record(drag["pdf_version_id"])["body"]
    assert pdf["mime"] == "application/pdf" and pdf["availability"] == "retained"
    assert pdf["sha256"] == hashlib.sha256(SMALL["drag/flywheel-drag.pdf"]).hexdigest()
    source = lib.record(drag["pdf_source_id"])["body"]
    assert source["root_binding"] == {"alias": "research", "path": "library/drag/flywheel-drag.pdf"}
    assert source["retention"] == "copy"
    text = lib.record(drag["text_version_id"])["body"]
    assert text["payload_schema"] == "pdf_pages_v1" and text["storage"] == "body"
    assert text["derived_from"]["version_id"] == drag["pdf_version_id"]
    assert text["derived_from"]["source_id"] == drag["pdf_source_id"]
    transformation = text["transformation"]
    assert transformation["type"] == "extraction" and transformation["engine"] == "pypdfium2"
    assert transformation["input_version_refs"] == [drag["pdf_version_id"]]
    assert transformation["parameters_digest"] == extract.parameters_digest()
    with closing(lib.db()) as conn:
        payload = library_index.load_payload(conn, text)
    assert payload is not None
    PdfPagesV1.model_validate(payload)
    assert payload["pdf_sha256"] == pdf["sha256"]
    assert [p["state"] for p in payload["pages"]] == ["text", "text", "no_text", "text"]

    work = lib.record(drag["work_id"])["body"]
    assert work["access_state"] == "full_text"
    assert work["full_text_refs"] == [{"id": drag["text_version_id"], "rev": 1}]
    assert [c["name"] for c in work["contributors"]] == ["A. Synthetic", "B. Example"]
    by_field = {p["field"]: p for p in work["field_provenance"]}
    assert by_field["title"]["basis"] == "pdf_info"
    assert by_field["title"]["locator"] == {"type": "json", "pointer": "/info/title"}
    page_one = payload["pages"][0]["text"]
    for field, value in (
        ("identifiers.doi", "10.5555/synthetic.2026.001"),
        ("identifiers.arxiv", "2601.01234"),
    ):
        loc = by_field[field]["locator"]
        assert loc["page"] == 1 and page_one[loc["start"] : loc["end"]].lower() == value
    # The abstract is a located, digest-checked evidence reference into page 1.
    abstract = work["abstract_ref"]
    span = page_one[abstract["locator"]["start"] : abstract["locator"]["end"]]
    assert span.startswith("We measure the drag coefficient") and span.endswith("10 Pa.")
    assert abstract["quote_sha256"] == sha(span)
    # A scan without a text layer is a work without full text (no OCR in S11.1).
    scan = lib.work("scan-without-text")
    assert scan["access_state"] == "metadata_only" and scan["pages_without_text"] == 2
    scan_body = lib.record(scan["work_id"])["body"]
    assert scan_body["field_provenance"][0]["basis"] == "file_name"


def test_same_bytes_give_no_new_records_and_a_commit_replays(lib: Lib) -> None:
    plan = lib.plan()
    first = lib.commit(plan)
    seq = lib.seq()
    again = lib.commit(plan)
    assert again["replayed"] is True and again["outcomes"] == first["outcomes"]
    replan = lib.plan()
    assert replan["counts"]["unchanged"] == 3 and replan["counts"]["create"] == 0
    assert lib.commit(replan)["counts"]["committed"] == 0
    # The same bytes copied under a new name are a duplicate of the known file; moved to a new
    # name they are recognised as the work they already are.
    (lib.folder / "new-name.pdf").write_bytes(SMALL["kers-review.pdf"])
    third = {i["path"]: i for i in lib.plan()["items"]}
    assert third["library/new-name.pdf"]["action"] == "duplicate"
    assert third["library/new-name.pdf"]["duplicate_of"] == "library/kers-review.pdf"
    (lib.folder / "kers-review.pdf").unlink()
    moved = {i["path"]: i for i in lib.plan()["items"]}["library/new-name.pdf"]
    assert moved["action"] == "unchanged" and moved["work_id"] == lib.work("Kinetic")["work_id"]
    assert lib.seq() == seq
    assert lib.svc.library_works(lib.pid, AGENT)["total"] == 3


def test_a_changed_file_revises_its_work(lib: Lib) -> None:
    lib.imported()
    before = lib.work("Kinetic")
    revised = pdfs.PaperSpec(
        title=pdfs.KERS.title, authors="E. Invented", arxiv="2602.04321",
        left=("1 Scope", "We review flywheel recovery systems."), right=("2 Findings", "Updated."),
    )  # fmt: skip
    (lib.folder / "kers-review.pdf").write_bytes(pdfs.paper(revised))
    plan = lib.plan()
    item = next(i for i in plan["items"] if i["path"] == "library/kers-review.pdf")
    assert item["action"] == "new_version" and item["work_id"] == before["work_id"]
    lib.commit(plan)
    after = lib.work("Kinetic")
    assert after["work_id"] == before["work_id"] and after["rev"] == 2
    assert after["pdf_source_id"] == before["pdf_source_id"]
    assert after["pdf_version_id"] != before["pdf_version_id"]
    hits = lib.svc.library_search(lib.pid, AGENT, "flywheel recovery")["hits"]
    assert [h["work_id"] for h in hits] == [after["work_id"]]
    assert lib.svc.library_search(lib.pid, AGENT, "Round-trip efficiency")["hits"] == []


def test_a_pdf_already_imported_as_a_source_is_extracted_without_a_new_version(lib: Lib) -> None:
    lib.env.import_files(["library/kers-review.pdf"])
    plan = lib.plan(path="library/kers-review.pdf")
    (item,) = plan["items"]
    assert item["action"] == "extract"
    lib.commit(plan)
    work = lib.work("Kinetic")
    with closing(lib.db()) as conn:
        versions = conn.execute("SELECT COUNT(*) FROM records WHERE kind = 'version'").fetchone()
    assert versions[0] == 2  # the source-imported PDF and the extracted text
    assert (
        lib.record(work["text_version_id"])["body"]["derived_from"]["version_id"]
        == (item["pdf_version_id"])
    )


def test_nothing_is_written_into_the_folder(lib: Lib) -> None:
    pdfs.write_folder(lib.folder, pdfs.hostile_set())
    before = (root_digest(lib.folder)[0], content_digest(lib.folder))
    plan = lib.plan()
    lib.commit(plan)
    lib.svc.library_search(lib.pid, AGENT, "drag")
    assert (root_digest(lib.folder)[0], content_digest(lib.folder)) == before


# ----- hostile files ----------------------------------------------------------------------------


def test_hostile_files_are_refused_with_reasons_or_kept_as_inert_text(lib: Lib) -> None:
    pdfs.write_folder(lib.folder, pdfs.hostile_set())
    plan = lib.plan()
    refused = {r["path"].removeprefix("library/hostile/"): r["reason"] for r in plan["refused"]}
    assert refused == {
        "malformed.pdf": "malformed",
        "truncated.pdf": "truncated",
        "encrypted.pdf": "encrypted",
        "page-bomb.pdf": "too_many_pages",
        "not-a-pdf.pdf": "not_pdf",
    }
    assert all(r["detail"] for r in plan["refused"])
    seq = lib.seq()
    lib.commit(plan)
    # The embedded instructions are imported as text and stay text: searchable, quoted with
    # the untrusted-data notice, and nothing else happened (only the import's own commits).
    hostile = lib.work("Notes on drag reduction")
    hits = lib.svc.library_search(lib.pid, AGENT, "IGNORE ALL PREVIOUS INSTRUCTIONS")["hits"]
    assert [h["work_id"] for h in hits] == [hostile["work_id"]]
    read = lib.svc.library_read(lib.pid, AGENT, hostile["work_id"], page=1)
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in read["text"]
    assert read["content_trust"] == "untrusted_data" and "no authority" in read["notice"]
    committed = lib.svc.library_works(lib.pid, AGENT)["total"]
    assert committed == 4
    with closing(lib.db()) as conn:
        kinds = {
            r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM records GROUP BY kind")
        }
    assert kinds["work"] == 4 and "entry" not in kinds and "claim" not in kinds
    assert lib.seq() == seq + 4  # one commit per imported PDF, nothing more


def test_a_link_inside_the_root_is_refused_not_followed(lib: Lib, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(pdfs.paper(pdfs.BATTERY))
    link = lib.folder / "linked"
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        if done.returncode != 0:
            pytest.skip("cannot create a junction here")
        file_link = None
    else:
        os.symlink(outside, link)
        file_link = lib.folder / "file-link.pdf"
        os.symlink(outside / "secret.pdf", file_link)
    plan = lib.plan()
    paths = [i["path"] for i in plan["items"]] + [r["path"] for r in plan["refused"]]
    assert not any("secret" in p for p in paths)  # the junction's folder is not entered
    if file_link is not None:
        assert {r["path"]: r["reason"] for r in plan["refused"]}["library/file-link.pdf"] == "link"


def test_a_file_over_the_size_limit_is_refused(lib: Lib, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "MAX_PDF_BYTES", 2000)
    plan = lib.plan()
    reasons = {r["path"]: r["reason"] for r in plan["refused"]}
    assert reasons["library/drag/flywheel-drag.pdf"] == "too_large"
    assert "library/scan-without-text.pdf" not in reasons  # 873 bytes


def test_an_extraction_crash_or_hang_is_a_refusal_not_a_failure(
    lib: Lib, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash = tmp_path / "crash.py"
    crash.write_text("import os\nos._exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(extract, "WORKER", crash)
    plan = lib.plan(path="library/kers-review.pdf")
    assert plan["items"] == []
    assert plan["refused"][0]["reason"] == "extract_failed"
    assert "code 3" in plan["refused"][0]["detail"]
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(extract, "WORKER", hang)
    monkeypatch.setattr(extract, "EXTRACT_SECONDS", 1.0)
    plan = lib.plan(path="library/kers-review.pdf")
    assert plan["refused"][0] == {
        "path": "library/kers-review.pdf", "reason": "extract_failed", "detail": "timed out"
    }  # fmt: skip


def test_a_plan_leaves_the_admission_gate_free_while_it_extracts(
    lib: Lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 0075: the plan run holds the project admission gate only for short store reads,
    never across extraction, so another thread's read is admitted at once (it used to wait for
    the whole read stage and, past five seconds, failed ``busy: project_admission``)."""
    real = extract.inspect_pdf
    extracting = threading.Event()

    def slow(data: bytes, sha256: str, limit: int) -> Any:
        extracting.set()
        time.sleep(1.5)
        extracting.clear()
        return real(data, sha256, limit)

    monkeypatch.setattr(extract, "inspect_pdf", slow)
    planned: list[Any] = []

    def plan() -> None:
        try:
            planned.append(lib.plan(wait_seconds=30))
        except BaseException as exc:  # re-raised below, in the test's own thread
            planned.append(exc)

    planner = threading.Thread(target=plan)
    planner.start()
    store = ProjectStore(lib.env.store_dir)
    waits = []
    try:
        while planner.is_alive():
            if not extracting.wait(0.05):
                continue
            started = time.monotonic()
            conn = store.connect(readonly=True)
            try:
                conn.execute("SELECT project_seq FROM project_state").fetchone()
            finally:
                conn.close()
            waits.append(time.monotonic() - started)
            time.sleep(0.6)  # once per extraction (1.5 s each)
    finally:
        planner.join(60)
    if planned and isinstance(planned[0], BaseException):
        raise planned[0]
    assert planned and planned[0]["counts"]["create"] == 3  # three PDFs extracted, slowly
    assert len(waits) >= 3, waits
    assert max(waits) < 0.5, waits  # before: blocked for the rest of the read stage


# ----- search, reads, quotations ----------------------------------------------------------------


def test_search_hits_are_located_exactly(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    result = lib.svc.library_search(lib.pid, AGENT, "drag coefficient")
    assert result["mode"] == "fts5" and result["content_trust"] == "untrusted_data"
    pages = {h["page"] for h in result["hits"] if h["work_id"] == drag["work_id"]}
    assert pages == {1, 2, 4}
    text_body = lib.record(drag["text_version_id"])["body"]
    with closing(lib.db()) as conn:
        payload = library_index.load_payload(conn, text_body)
    assert payload is not None
    for hit in result["hits"]:
        assert hit["end"] - hit["start"] == len("drag coefficient")
        if hit["work_id"] != drag["work_id"]:
            continue
        text = payload["pages"][hit["page"] - 1]["text"]
        assert text[hit["start"] : hit["end"]] == "drag coefficient"
        assert hit["snippet"]["match"] == "drag coefficient"
        assert hit["quote_sha256"] == sha("drag coefficient")
        assert hit["evidence"]["version_id"] == drag["pdf_version_id"]
        assert hit["evidence"]["locator"] == {
            "type": "pdf", "page": hit["page"], "bbox": None, "start": hit["start"],
            "end": hit["end"],
        }  # fmt: skip
    # Case and spacing do not matter; the span is the text as stored.
    upper = lib.svc.library_search(lib.pid, AGENT, "DRAG   Coefficient", work_id=drag["work_id"])
    assert {h["page"] for h in upper["hits"]} == {1, 2, 4}
    assert lib.svc.library_search(lib.pid, AGENT, "nonexistentword")["hits"] == []
    with pytest.raises(PrometheosError) as empty:
        lib.svc.library_search(lib.pid, AGENT, "?!")
    assert empty.value.code == "validation_error"
    # The record-level knowledge search finds the work by its title.
    titles = [h["title"] for h in lib.svc.search(lib.pid, AGENT, "flywheel housing")["hits"]]
    assert "Aerodynamic drag of a flywheel housing at low pressure" in titles


def test_read_is_bounded_and_located(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    first = lib.svc.library_read(lib.pid, AGENT, drag["work_id"], page=1, max_chars=50)
    assert first["start"] == 0 and first["end"] == 50 and len(first["text"]) == 50
    assert first["next"] == {"page": 1, "start": 50}
    assert first["quote_sha256"] == sha(first["text"])
    assert first["evidence"]["locator"] == {
        "type": "pdf", "page": 1, "bbox": None, "start": 0, "end": 50
    }  # fmt: skip
    rest = lib.svc.library_read(lib.pid, AGENT, drag["work_id"], page=1, start=50, max_chars=8000)
    assert rest["next"] == {"page": 2, "start": 0} and rest["end"] == rest["page_chars"]
    whole = lib.svc.library_read(lib.pid, AGENT, drag["work_id"], page=1, max_chars=8000)
    assert first["text"] + rest["text"] == whole["text"]
    clamped = lib.svc.library_read(
        lib.pid, AGENT, drag["work_id"], page=1, start=0, end=whole["end"], max_chars=10
    )
    assert clamped["clamped"] is True and len(clamped["text"]) == 10
    blank = lib.svc.library_read(lib.pid, AGENT, drag["work_id"], page=3)
    assert blank["state"] == "no_text" and blank["text"] == "" and "no text layer" in blank["note"]
    assert blank["locator"] is None
    last = lib.svc.library_read(lib.pid, AGENT, drag["work_id"], page=4, max_chars=8000)
    assert last["next"] is None
    for bad in ({"page": 5}, {"page": 1, "start": 99999}, {"page": 1, "start": 5, "end": 4}):
        with pytest.raises(PrometheosError) as refused:
            lib.svc.library_read(lib.pid, AGENT, drag["work_id"], **bad)
        assert refused.value.code == "validation_error", bad
    with pytest.raises(PrometheosError) as refused:
        lib.svc.library_read(lib.pid, AGENT, drag["work_id"], max_chars=8001)
    assert refused.value.code == "validation_error"


def test_quote_check_matches_or_returns_the_true_text(lib: Lib) -> None:
    lib.imported()
    hit = next(
        h
        for h in lib.svc.library_search(lib.pid, AGENT, "0.0042")["hits"]
        if h["title"].startswith("Aerodynamic")
    )
    where = {"page": hit["page"], "start": hit["start"], "end": hit["end"]}
    good = lib.svc.library_quote_check(
        lib.pid, AGENT, work_id=hit["work_id"], version_id=None, quote="0.0042",
        quote_sha256=None, **where,
    )  # fmt: skip
    assert good["result"] == "match" and good["true_text"] == "0.0042"
    by_digest = lib.svc.library_quote_check(
        lib.pid, AGENT, work_id=None, version_id=hit["pdf_version_id"], quote=None,
        quote_sha256=sha("0.0042"), **where,
    )  # fmt: skip
    assert by_digest["result"] == "match"
    altered = lib.svc.library_quote_check(
        lib.pid, AGENT, work_id=hit["work_id"], version_id=None, quote="0.042",
        quote_sha256=None, **where,
    )  # fmt: skip
    assert altered["result"] == "mismatch" and altered["true_text"] == "0.0042"
    assert altered["given_sha256"] == sha("0.042") and altered["true_sha256"] == sha("0.0042")
    # A correct quotation at the wrong place is a mismatch that says where the text is.
    moved = lib.svc.library_quote_check(
        lib.pid, AGENT, work_id=hit["work_id"], version_id=None, quote="0.0042",
        quote_sha256=None, page=1, start=0, end=6,
    )  # fmt: skip
    assert moved["result"] == "mismatch"
    assert {"page": hit["page"], "start": hit["start"], "end": hit["end"]} in moved["found_at"]
    with pytest.raises(PrometheosError) as outside:
        lib.svc.library_quote_check(
            lib.pid, AGENT, work_id=hit["work_id"], version_id=None, quote="x",
            quote_sha256=None, page=hit["page"], start=0, end=99999,
        )  # fmt: skip
    assert outside.value.code == "validation_error"


# ----- PDF locators in evidence (check_evidence) -------------------------------------------------


def cite(lib: Lib, evidence: dict[str, Any]) -> dict[str, Any]:
    return dict(lib.env.apply([create_entry(title="Cites a paper", evidence=[evidence])]))


def test_check_evidence_validates_pdf_locators_against_the_page_text(lib: Lib) -> None:
    lib.imported()
    hit = next(
        h
        for h in lib.svc.library_search(lib.pid, AGENT, "drag coefficient")["hits"]
        if h["title"].startswith("Aerodynamic") and h["page"] == 2
    )
    assert cite(lib, hit["evidence"])["ok"]
    loc = hit["evidence"]["locator"]
    # The text version may be cited directly too.
    text_source = lib.record(hit["text_version_id"])["body"]["source_id"]
    direct = {**hit["evidence"], "source_id": text_source, "version_id": hit["text_version_id"]}
    assert cite(lib, direct)["ok"]
    # Mutation spot-checks: any shift of the span, a wrong page or a wrong digest is refused.
    mutants = [
        {**loc, "start": loc["start"] + 1},
        {**loc, "end": loc["end"] - 1},
        {**loc, "start": loc["start"] - 1, "end": loc["end"] - 1},
        {**loc, "page": 1},
    ]
    for mutant in mutants:
        with pytest.raises(PrometheosError) as refused:
            cite(lib, {**hit["evidence"], "locator": mutant})
        assert refused.value.code == "validation_error", mutant
        assert "quote digest" in refused.value.message
    for bad, message in (
        ({**loc, "page": 9}, "page is outside"),
        ({**loc, "start": 0, "end": 10_000}, "outside the page text"),
    ):
        with pytest.raises(PrometheosError) as refused:
            cite(lib, {**hit["evidence"], "locator": bad, "quote_sha256": None})
        assert message in refused.value.message
    with pytest.raises(PrometheosError) as refused:
        no_offsets = {"type": "pdf", "page": 2, "bbox": None}
        cite(lib, {**hit["evidence"], "locator": no_offsets})
    assert "needs start and end" in refused.value.message
    # Without offsets and digest a page is checked for range only (older citations stay valid).
    assert cite(lib, {**hit["evidence"], "locator": {"type": "pdf", "page": 3, "bbox": None},
                      "quote_sha256": None})["ok"]  # fmt: skip


def test_offsets_need_extracted_text(lib: Lib) -> None:
    lib.env.import_files(["library/kers-review.pdf"])
    commit = lib.env.import_files(["library/drag/flywheel-drag.pdf"])["commit"]
    source_id, version_id = (r["id"] for r in commit["results"])
    plain = {"source_id": source_id, "version_id": version_id, "quote_sha256": None}
    assert cite(lib, {**plain, "locator": {"type": "pdf", "page": 7, "bbox": None}})["ok"]
    with pytest.raises(PrometheosError) as refused:
        cite(
            lib,
            {**plain, "locator": {"type": "pdf", "page": 1, "bbox": None, "start": 0, "end": 4}},
        )
    assert "extracted text" in refused.value.message


# ----- visibility --------------------------------------------------------------------------------


def test_works_above_the_callers_scope_read_as_absent(lib: Lib) -> None:
    lib.commit(lib.plan(sensitivity="restricted"))
    assert lib.svc.library_works(lib.pid, AGENT)["total"] == 0
    assert lib.svc.library_search(lib.pid, AGENT, "drag")["hits"] == []
    owner = lib.svc.library_works(lib.pid, OWNER)["works"]
    assert len(owner) == 3 and {w["sensitivity"] for w in owner} == {"restricted"}
    with pytest.raises(PrometheosError) as hidden:
        lib.svc.library_read(lib.pid, AGENT, owner[0]["work_id"])
    assert hidden.value.code == "not_found"
    # An agent's plan of the same bytes names nothing about them.
    plan = lib.plan(caller=AGENT)
    assert {r["reason"] for r in plan["refused"]} == {"owner_scope"}
    assert owner[0]["work_id"] not in json.dumps(plan)
    with pytest.raises(PrometheosError) as above:
        lib.plan(caller=AGENT, sensitivity="restricted")
    assert above.value.code == "policy_denied"


def test_an_agent_plan_commits_only_at_its_rank(lib: Lib) -> None:
    plan = lib.plan(caller=OWNER)
    with pytest.raises(PrometheosError) as absent:
        lib.commit(plan, caller=AGENT)
    assert absent.value.code == "not_found"
    agent_plan = lib.plan(caller=AGENT)
    assert lib.commit(agent_plan, caller=AGENT)["counts"]["committed"] == 3


def test_works_are_registered_only_by_the_importer(lib: Lib) -> None:
    lib.imported()
    work = lib.work("Aerodynamic")
    body = lib.record(work["work_id"])["body"]
    op = {"op": "create", "kind": "work", "id": "work_00000000-0000-4000-8000-000000000001",
          "base_rev": 0, "sensitivity": "normal", "occurred_at": None, "body": body}  # fmt: skip
    with pytest.raises(PrometheosError) as refused:
        lib.env.apply([op])
    assert refused.value.code == "policy_denied" and "library import" in refused.value.message
    revise = {"op": "revise", "id": work["work_id"], "base_rev": 1, "sensitivity": "normal",
              "occurred_at": None, "body": {**body, "title": "Renamed"}}  # fmt: skip
    with pytest.raises(PrometheosError) as revised:
        lib.env.apply([revise])
    assert revised.value.code == "policy_denied"


# ----- derived tables: rebuild, purge, migration 13 ----------------------------------------------


def snapshot(lib: Lib) -> tuple[Any, ...]:
    with closing(lib.db()) as conn:
        pages = conn.execute(
            "SELECT version_id, page, state, text FROM library_pages ORDER BY version_id, page"
        ).fetchall()
        works = conn.execute("SELECT * FROM library_works ORDER BY work_id").fetchall()
    return tuple(tuple(r) for r in pages), tuple(tuple(r) for r in works)


def test_the_page_index_is_rebuilt_from_records(lib: Lib) -> None:
    lib.imported()
    before = snapshot(lib)
    hits = lib.svc.library_search(lib.pid, AGENT, "windage")["hits"]
    with closing(lib.db()) as conn:
        conn.execute("DELETE FROM library_pages")
        conn.execute("DELETE FROM library_works")
        conn.execute("INSERT INTO library_fts (library_fts) VALUES ('rebuild')")
        conn.commit()
    assert lib.svc.library_search(lib.pid, AGENT, "windage")["hits"] == []
    from prometheos.store import derived

    with closing(lib.db()) as conn:
        derived.rebuild(conn)
        conn.commit()
    assert snapshot(lib) == before
    assert lib.svc.library_search(lib.pid, AGENT, "windage")["hits"] == hits


def test_purging_a_pdf_removes_its_text_work_and_index_rows(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    plan = lib.svc.purge_plan(lib.pid, [drag["pdf_source_id"]], OWNER)
    closure = set(plan["closure"])
    text_source = lib.record(drag["text_version_id"])["body"]["source_id"]
    assert {drag["pdf_source_id"], drag["pdf_version_id"], drag["text_version_id"], text_source,
            drag["work_id"]} <= closure  # fmt: skip
    grant = lib.svc.grant(
        lib.pid, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    lib.svc.purge_commit(lib.pid, plan["plan_id"], grant.secret)
    assert lib.svc.library_search(lib.pid, OWNER, "windage")["hits"] == []
    with closing(lib.db()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM library_pages WHERE version_id = ?",
                (drag["text_version_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM library_works WHERE work_id = ?", (drag["work_id"],)
            ).fetchone()[0]
            == 0
        )
        # No FTS token of the purged text survives (the index was rebuilt).
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'windage'"
            ).fetchone()[0]
            == 0
        )
    assert lib.svc.library_works(lib.pid, OWNER)["total"] == 2


def test_migration_13_upgrades_a_schema_12_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.store import project

    assert MIGRATIONS[12].version == 13 and "library" in MIGRATIONS[12].name
    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", MIGRATIONS[:12])
        lib = Lib(tmp_path)
        lib.env.import_files(["library/kers-review.pdf"])  # a PDF registered under schema 12
    with closing(lib.db()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        assert not library_index.installed(conn)
    result = lib.svc.migrate(lib.pid, authority=OWNER_ACT)
    assert result["applied"] == list(range(13, len(MIGRATIONS) + 1))
    with closing(lib.db()) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"library_plans", "library_pages", "library_works", "library_fts"} <= tables
    plan = lib.plan(path="library/kers-review.pdf")
    assert [i["action"] for i in plan["items"]] == ["extract"]
    lib.commit(plan)
    assert [h["page"] for h in lib.svc.library_search(lib.pid, AGENT, "efficiency")["hits"]] == [1]


# ----- the CLI ----------------------------------------------------------------------------------


def test_cli_plans_commits_searches_reads_and_checks(tmp_path: Path) -> None:
    from test_cp1_cli import Cli

    lib = Lib(tmp_path)
    cli = Cli(tmp_path)
    base = ("--project", lib.pid)
    code, plan, err = cli(
        "library", "import", "--plan", "--root", "research", "--path", "library", *base
    )
    assert code == 0, err
    code, done, err = cli("library", "import", "--commit", plan["plan_id"], *base)
    assert code == 0, err
    assert done["counts"]["committed"] == 3
    code, found, _ = cli("library", "search", "windage loss", *base)
    hit = found["hits"][0]
    code, read, _ = cli("library", "read", hit["work_id"], "--page", str(hit["page"]), *base)
    assert hit["snippet"]["match"].lower() in read["text"].lower()
    where = ("--page", str(hit["page"]), "--start", str(hit["start"]), "--end", str(hit["end"]))
    quote = ("library", "quote", "check", "--work", hit["work_id"], *where)
    code, checked, _ = cli(*quote, "--quote", hit["snippet"]["match"], *base)
    assert code == 0 and checked["result"] == "match"
    code, checked, _ = cli(*quote, "--quote", "not the text", *base)
    assert code == 0 and checked["result"] == "mismatch"
    code, works, _ = cli("library", "works", *base)
    assert works["total"] == 3


# ----- MCP and HTTP: the long operations ---------------------------------------------------------


def _mcp(lib: Lib, tool: str, args: dict[str, Any]) -> Any:
    import anyio
    from mcp import Client

    from prometheos.mcp_adapter import build_server

    async def go() -> Any:
        async with Client(build_server(lib.svc, lib.pid)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def _by_mcp(lib: Lib) -> tuple[Any, Any, list[Any]]:
    args = {"root_alias": "research", "path": "library", "wait_seconds": 20}
    planned = _mcp(lib, "library_import_plan", args)
    plan_id = planned["result"]["plan_id"]
    done = _mcp(lib, "library_import_commit", {"plan_id": plan_id, "wait_seconds": 20})
    found = _mcp(lib, "library", {"request": {"op": "search", "query": "windage"}})
    return planned, done, found["result"]["hits"]


def _by_http(lib: Lib) -> tuple[Any, Any, list[Any]]:
    from hub_support import running_hub

    with running_hub(lib.svc, lib.pid) as hub:
        hub.pair()
        headers = {"Authorization": f"Bearer {hub.gate.new_agent_token()}"}

        def call(method: str, path: str, body: Any = None) -> Any:
            reply = hub.request(
                method, path, body, cookie=None, csrf=None, origin=None, headers=headers
            )
            assert reply.status == 200, reply.json()
            return reply.json()

        args = {"root_alias": "research", "path": "library", "wait_seconds": 20}
        planned = call("POST", "/api/v1/library/plans", args)
        plan_id = planned["result"]["plan_id"]
        done = call("POST", f"/api/v1/library/plans/{plan_id}/commit", {"wait_seconds": 20})
        hits = call("GET", "/api/v1/library/search?query=windage")["hits"]
    return planned, done, hits


def test_mcp_and_http_plan_and_commit_alike(tmp_path: Path) -> None:
    outcomes = {}
    for route, run in (("mcp", _by_mcp), ("http", _by_http)):
        folder = tmp_path / route
        folder.mkdir()
        planned, done, hits = run(Lib(folder))
        assert planned["state"] == "completed" and done["state"] == "completed"
        assert planned["operation"] == "library_import_plan"
        outcomes[route] = (
            planned["result"]["counts"],
            done["result"]["counts"],
            [(h["page"], h["start"], h["end"], h["quote_sha256"]) for h in hits],
        )
    assert outcomes["mcp"] == outcomes["http"]
    assert outcomes["mcp"][1]["committed"] == 3 and outcomes["mcp"][2]


# ----- review round 1 (review-precheck-s11-1-a) regressions ---------------------------------------


def revise_entry(entry_id: str, rev: int, **body: Any) -> dict[str, Any]:
    from cp1_support import entry_body

    return {
        "op": "revise",
        "id": entry_id,
        "base_rev": rev,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": entry_body(**body),
    }


def test_f1_an_older_pdf_citation_stays_revisable_after_library_import(lib: Lib) -> None:
    """An entry citing a PDF page with a quote digest and no offsets (valid before the PDF had
    extracted text) can still be revised once the PDF is library-imported; new citations of
    that form, and changed ones, are held to the new rules."""
    from prometheos.domain.ids import new_id

    commit = lib.env.import_files(["library/kers-review.pdf"])["commit"]
    source_id, version_id = (r["id"] for r in commit["results"])
    old = {
        "source_id": source_id,
        "version_id": version_id,
        "locator": {"type": "pdf", "page": 1, "bbox": None},
        "quote_sha256": sha("We review kinetic energy recovery systems."),
    }
    entry_id = new_id("entry")
    lib.env.apply([create_entry(record_id=entry_id, title="Old note", evidence=[old])])
    lib.commit(lib.plan(path="library/kers-review.pdf"))  # action "extract"
    revised = lib.env.apply([revise_entry(entry_id, 1, title="Renamed", evidence=[old])])
    assert revised["ok"]
    with pytest.raises(PrometheosError) as fresh:
        cite(lib, old)  # the same form, cited anew, is refused
    assert "needs start and end" in fresh.value.message
    changed = {**old, "locator": {"type": "pdf", "page": 2, "bbox": None}, "quote_sha256": None}
    with pytest.raises(PrometheosError) as moved:
        lib.env.apply([revise_entry(entry_id, 2, title="Renamed", evidence=[changed])])
    assert "page is outside" in moved.value.message  # a changed citation is checked


def test_f2_the_child_stops_at_the_cumulative_text_cap(
    lib: Lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contracts, "MAX_TEXT_BYTES", 200)
    plan = lib.plan(path="library/drag/flywheel-drag.pdf")
    (refused,) = plan["refused"]
    assert refused["reason"] == "text_too_large"
    assert "characters by page" in refused["detail"]  # decided in the child, before extraction
    assert extract.max_total_chars() == 400


def test_f3_information_dictionary_strings_lose_control_characters(lib: Lib) -> None:
    page = pdfs.Page([pdfs.Run(72, 700, "Body text of a synthetic note.")])
    # BEL, VT and NUL (PDFDocEncoding maps 0x18-0x1F to accents, so those are not controls).
    info = {"Title": "Good Title\x07 end\x0b[31m", "Author": "A. B\x00C"}
    (lib.folder / "controls.pdf").write_bytes(pdfs.build([page], info=info))
    lib.commit(lib.plan(path="library/controls.pdf"))
    work = lib.work("Good Title")
    assert work["title"] == "Good Title end[31m"
    assert work["contributors"] == ["A. BC"]
    body = lib.record(work["text_version_id"])["body"]
    with closing(lib.db()) as conn:
        payload = library_index.load_payload(conn, body)
    assert payload is not None and payload["info"]["title"] == "Good Title end[31m"


def test_f5_boundaries_page_end_page_count_and_line_ends(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    text_body = lib.record(drag["text_version_id"])["body"]
    with closing(lib.db()) as conn:
        payload = library_index.load_payload(conn, text_body)
    assert payload is not None
    # CR LF from PDFium is stored as LF: no CR anywhere, lines as on the page.
    assert all("\r" not in p["text"] for p in payload["pages"])
    assert payload["pages"][1]["text"].startswith("3 Results\nThe drag coefficient was 0.0042")
    from prometheos.library.text import normalise_page_text

    assert normalise_page_text("a\r\nb\rc\x00d \n") == "a\nb\ncd"
    # A span that ends exactly at the end of the page is valid.
    page = payload["pages"][3]["text"]
    tail = {"type": "pdf", "page": 4, "bbox": None, "start": len(page) - 6, "end": len(page)}
    ev = {
        "source_id": drag["pdf_source_id"],
        "version_id": drag["pdf_version_id"],
        "locator": tail,
        "quote_sha256": sha(page[-6:]),
    }
    assert cite(lib, ev)["ok"]
    beyond = {**tail, "start": len(page) - 5, "end": len(page) + 1}
    with pytest.raises(PrometheosError) as past:
        cite(lib, {**ev, "locator": beyond, "quote_sha256": None})
    assert "outside the page text" in past.value.message
    # The page after the last one is outside the document (page_count + 1).
    last = {"type": "pdf", "page": 4, "bbox": None}
    with pytest.raises(PrometheosError) as after:
        cite(lib, {**ev, "locator": {**last, "page": 5}, "quote_sha256": None})
    assert "page is outside" in after.value.message
    assert cite(lib, {**ev, "locator": last, "quote_sha256": None})["ok"]


def test_f5_a_file_changed_after_the_plan_is_skipped_and_the_rest_commit(lib: Lib) -> None:
    plan = lib.plan()
    (lib.folder / "kers-review.pdf").write_bytes(pdfs.paper(pdfs.BATTERY))
    result = lib.commit(plan)
    outcomes = {o["path"]: o for o in result["outcomes"]}
    assert outcomes["library/kers-review.pdf"]["outcome"] == "skipped"
    assert outcomes["library/kers-review.pdf"]["reason"] == "changed"
    assert result["counts"]["committed"] == 2 and result["counts"]["skipped"] == 1


def test_f5_a_reference_only_source_is_refused(lib: Lib) -> None:
    lib.env.import_files(["library/kers-review.pdf"], retention="reference")
    plan = lib.plan(path="library/kers-review.pdf")
    assert plan["items"] == []
    assert plan["refused"][0]["reason"] == "reference_only"


def test_f5_an_interrupted_commit_resumes_and_replays(lib: Lib) -> None:
    from prometheos.library import importer

    plan = lib.plan()

    def stop_at_second(stage: str) -> None:
        if stage.startswith("file 2 of"):
            raise RuntimeError("interrupted")

    store = lib.svc.open(lib.pid)
    with pytest.raises(RuntimeError):
        importer.commit_plan(store, plan["plan_id"], "test", max_rank=2, checkpoint=stop_at_second)
    assert lib.svc.library_works(lib.pid, OWNER)["total"] == 1
    seq = lib.seq()
    result = lib.commit(plan)
    committed = [o for o in result["outcomes"] if o["outcome"] == "committed"]
    assert [o["replayed"] for o in committed] == [True, False, False]
    assert lib.seq() == seq + 2  # the first file's commit was replayed, not repeated
    assert lib.svc.library_works(lib.pid, OWNER)["total"] == 3


def test_f5_an_expired_plan_is_refused(lib: Lib, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from prometheos.library import importer

    plan = lib.plan()
    later = importer._now() + timedelta(hours=2)
    monkeypatch.setattr(importer, "_now", lambda: later)
    with pytest.raises(PrometheosError) as stale:
        importer.commit_plan(lib.svc.open(lib.pid), plan["plan_id"], "test", max_rank=2)
    assert stale.value.code == "stale_task"


def test_f5_a_large_pdf_is_kept_in_media(lib: Lib) -> None:
    page = pdfs.Page([pdfs.Run(72, 700, "A large synthetic file with little text.")])
    data = pdfs.build([page], info={"Title": "Large synthetic file"}, padding=9 * 1024 * 1024)
    (lib.folder / "large.pdf").write_bytes(data)
    lib.commit(lib.plan(path="library/large.pdf"))
    work = lib.work("Large synthetic")
    pdf = lib.record(work["pdf_version_id"])["body"]
    assert pdf["storage"] == "media" and pdf["byte_length"] == len(data)
    stored = lib.env.store_dir / "media" / pdf["storage_key"]
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == pdf["sha256"]
    assert not any((lib.env.store_dir / "staging").iterdir())
    read = lib.svc.library_read(lib.pid, AGENT, work["work_id"])
    assert read["text"] == "A large synthetic file with little text."


def test_f5_a_page_over_the_page_limit_is_kept_without_text(
    lib: Lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contracts, "MAX_PAGE_CHARS", 50)
    plan = lib.plan(path="library/drag/flywheel-drag.pdf")
    (item,) = plan["items"]
    assert 1 in item["pages_without_text"]  # page 1 is longer than twice the limit


def test_f6_restricted_text_over_a_normal_pdf_is_no_oracle(lib: Lib) -> None:
    """Text extracted at a higher sensitivity than its (already registered) PDF reads, for a
    caller below it, exactly as if no text existed: no page length, no digest confirmation, no
    existence, no ID."""
    commit = lib.env.import_files(["library/kers-review.pdf"])["commit"]
    source_id, version_id = (r["id"] for r in commit["results"])
    lib.commit(lib.plan(path="library/kers-review.pdf", sensitivity="restricted"))
    work = lib.svc.library_works(lib.pid, OWNER)["works"][0]
    text = lib.record(work["text_version_id"])
    assert text["sensitivity"] == "restricted"
    assert lib.record(version_id)["sensitivity"] == "normal"
    word = "Kinetic"
    good = {
        "source_id": source_id,
        "version_id": version_id,
        "locator": {"type": "pdf", "page": 1, "bbox": None, "start": 0, "end": len(word)},
        "quote_sha256": sha(word),
    }
    answers = []
    for attempt in (good, {**good, "locator": {**good["locator"], "end": 99_999}}):
        with pytest.raises(PrometheosError) as refused:
            lib.env.apply([create_entry(title="Probe", evidence=[attempt])], caller=AGENT)
        answers.append((refused.value.code, refused.value.message))
    assert answers[0] == answers[1]
    assert "extracted text" in answers[0][1]
    unknown = "version_00000000-0000-4000-8000-000000000009"
    replies = []
    for vid in (version_id, unknown):
        with pytest.raises(PrometheosError) as absent:
            lib.svc.library_quote_check(
                lib.pid, AGENT, work_id=None, version_id=vid, page=1, start=0, end=7,
                quote=word, quote_sha256=None,
            )  # fmt: skip
        replies.append((absent.value.code, absent.value.message, absent.value.details))
    assert replies[0][:2] == replies[1][:2] == ("not_found", "no extracted text for this version")
    assert replies[0][2] == {"id": version_id}
    assert work["text_version_id"] not in json.dumps(replies)


def test_f8_the_works_list_says_its_text_is_untrusted(lib: Lib) -> None:
    lib.imported()
    works = lib.svc.library_works(lib.pid, AGENT)
    assert works["content_trust"] == "untrusted_data" and "no authority" in works["notice"]
