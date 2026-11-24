"""CP11 S11.2: bibliographies, identifiers, duplicates and versions (decisions 0062, 0063).

Behaviour tests for the bibliography clauses of C11-IMPORT and every clause of C11-DEDUP, on
synthetic files only (``tools/synthetic_bib.py``, ``tools/synthetic_pdf.py``):

- import plans and commits of BibTeX, RIS and CSV keep the original bytes and give every field
  a located provenance record; identifiers are normalised and invalid ones refused per field;
- exact identifier duplicates are one work within an import, across imports and against PDF
  works, with every provenance record kept and a recorded field precedence; conflicting
  identifiers are refused for review;
- similar works are only proposed; merge, distinct and versions are explicit owner choices
  (approval_required from an agent); a distinct choice is remembered;
- preprint and published versions are related, never merged;
- the same bytes create nothing; a changed file revises only the changed entries;
- exports round-trip in all three formats; CSV cells cannot start a formula;
- annotations cite verified PDF spans and are exported;
- hostile and malformed inputs are refused with reasons or stay inert text.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import synthetic_bib as bibs
import synthetic_pdf as pdfs
from cp1_support import AGENT, OWNER, OWNER_ACT, Env

from prometheos.domain.models import PrometheosError
from prometheos.library import biblio, dedup, export
from prometheos.library import identifiers as ids
from prometheos.store.schema import MIGRATIONS


def grant(env: Env, approval_id: str) -> None:
    shown = env.svc.approval_owner_view(env.project_id, approval_id)
    env.svc.approval_decide(
        env.project_id, approval_id, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )


class Lib:
    def __init__(self, tmp: Path, *, pdfs_first: bool = True) -> None:
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.folder = self.env.research / "library"
        if pdfs_first:
            pdfs.write_folder(
                self.folder, {"drag.pdf": pdfs.paper(pdfs.DRAG), "kers.pdf": pdfs.paper(pdfs.KERS)}
            )
        bibs.write_folder(self.folder, bibs.synthetic_set())

    def plan(self, path: str = "library", caller: Any = OWNER, **kw: Any) -> dict[str, Any]:
        return dict(self.svc.library_import_plan(self.pid, caller, root_alias="research",
                                                 path=path, **kw))  # fmt: skip

    def commit(self, plan: dict[str, Any], caller: Any = OWNER) -> dict[str, Any]:
        return dict(self.svc.library_import_commit(self.pid, plan["plan_id"], caller))

    def imported(self, path: str = "library") -> dict[str, Any]:
        return self.commit(self.plan(path))

    def works(self, caller: Any = AGENT, **kw: Any) -> list[dict[str, Any]]:
        return list(self.svc.library_works(self.pid, caller, limit=200, **kw)["works"])

    def work(self, title_start: str) -> dict[str, Any]:
        return next(w for w in self.works() if w["title"].lower().startswith(title_start.lower()))

    def body(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None)["body"])

    def record(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None))

    def seq(self) -> int:
        return int(self.env.seq())

    def revs(self) -> dict[str, int]:
        return {w["work_id"]: w["rev"] for w in self.works(OWNER, include_merged=True)}


@pytest.fixture
def lib(tmp_path: Path) -> Lib:
    return Lib(tmp_path)


def entries(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for item in plan["items"] for e in item.get("entries", [])}


def file_text(lib: Lib, version_id: str) -> list[str]:
    body = lib.body(version_id)
    with closing(sqlite3.connect(lib.env.store_dir / "record.sqlite")) as conn:
        data = conn.execute("SELECT data FROM payloads WHERE sha256 = ?", (body["sha256"],))
        raw = bytes(data.fetchone()[0])
    return raw.decode("utf-8").split("\n")


# ----- identifiers -------------------------------------------------------------------------------


def test_identifiers_are_normalised_and_invalid_ones_refused() -> None:
    assert ids.doi("https://doi.org/10.5555/ABC.1") == "10.5555/abc.1"
    assert ids.doi("doi:10.5555/x") == ids.doi("http://dx.doi.org/10.5555/X") == "10.5555/x"
    assert ids.arxiv("arXiv:2603.00042v2") == ("2603.00042", "v2")
    assert ids.arxiv("https://arxiv.org/abs/hep-th/9901001v1") == ("hep-th/9901001", "v1")
    assert ids.pmid("PMID: 12345") == "12345" and ids.pmcid("pmc 7654321") == "PMC7654321"
    assert ids.isbn("0-306-40615-2") == "9780306406157"  # ISBN-10 converted
    assert ids.isbn("978-0-306-40615-7") == "9780306406157"
    for call, value in (
        (ids.doi, "10.5555"), (ids.doi, "10.1/a 10.1/b"), (ids.arxiv, "2613.00001"),
        (ids.pmid, "0123"), (ids.pmcid, "7654321"), (ids.isbn, "978-0-306-40615-8"),
        (ids.isbn, "0-306-40615-3"),
    ):  # fmt: skip
        with pytest.raises(ids.Invalid):
            call(value)


# ----- import -------------------------------------------------------------------------------------


def test_files_are_kept_and_every_field_has_a_located_provenance_record(lib: Lib) -> None:
    result = lib.imported()
    bib = next(o for o in result["outcomes"] if o["path"] == "library/refs.bib")
    version = lib.body(bib["version_id"])
    data = (lib.folder / "refs.bib").read_bytes()
    assert version["sha256"] == hashlib.sha256(data).hexdigest()
    assert version["availability"] == "retained" and version["payload_schema"] == "text"
    assert version["mime"] == "application/x-bibtex"
    lines = file_text(lib, bib["version_id"])
    work = lib.work("Windage in sealed housings: a survey")
    body = lib.body(work["work_id"])
    records = [r for r in body["field_provenance"] if r["basis"] == "bibliography"]
    assert {r["field"] for r in records} >= {"title", "contributors", "year", "venue"}
    for record in records:
        loc = record["locator"]
        cited = "\n".join(lines[loc["start_line"] - 1 : loc["end_line"]])
        assert (
            record["entry"] == "doe2024survey" and record["version_ref"]["id"] == bib["version_id"]
        )
        if record["field"] in ("title", "venue", "year"):
            assert record["value"] in cited, record
    # The cited lines check out as evidence (a text locator on the retained file).
    cite = {"source_id": bib["source_id"], "version_id": bib["version_id"],
            "locator": records[0]["locator"], "quote_sha256": None}  # fmt: skip
    from cp1_support import create_entry

    assert lib.env.apply([create_entry(title="Cites a field", evidence=[cite])])["ok"]


def test_identifiers_in_files_are_normalised(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    assert drag["identifiers"]["doi"] == "10.5555/synthetic.2026.001"  # https://doi.org/ removed
    preprint = next(w for w in lib.works() if w["identifiers"].get("arxiv") == "2603.00042")
    other = lib.body(preprint["work_id"])["identifiers"]["other"]
    assert {"scheme": "arxiv_version", "value": "v2"} in other  # the version kept apart
    assert lib.work("Flywheels: a synthetic")["identifiers"]["isbn"] == "9780306406157"
    assert lib.work("Synthetic rotor safety")["identifiers"]["pmcid"] == "PMC7654321"
    assert lib.work("Flywheel energy storage in")["identifiers"]["pmid"] == "12345678"


def test_csv_columns_are_matched_declared_and_unmapped_ones_reported(lib: Lib) -> None:
    plan = lib.plan("library/refs.csv")
    (item,) = plan["items"]
    assert item["unmapped_columns"] == ["notes"]
    assert item["column_map"]["authors"] == "contributors"
    (lib.folder / "custom.csv").write_text(
        "Heading,Writers,Published\nDeclared mapping paper,Doe J.,2021\n", encoding="utf-8"
    )
    declared = lib.plan("library/custom.csv", csv_columns={
        "Heading": "title", "Writers": "contributors", "Published": "year"})  # fmt: skip
    (entry,) = declared["items"][0]["entries"]
    assert entry["action"] == "create" and entry["title"] == "Declared mapping paper"
    with pytest.raises(PrometheosError):
        lib.plan("library/custom.csv", csv_columns={"Heading": "rm -rf"})


def test_csv_formula_cells_are_escaped_on_export_and_restored_on_import(lib: Lib) -> None:
    lib.imported()
    formula = lib.work('=HYPERLINK("')
    assert formula["title"] == '=HYPERLINK("http://evil.example","click")'  # stored inert
    out = lib.svc.library_export(lib.pid, AGENT, "csv", [formula["work_id"]])["content"]
    row = out.splitlines()[1]
    assert ",\"'=HYPERLINK(" in row or row.split(",")[2].startswith("'=")
    assert export.escape_cell("-1+2") == "'-1+2" and export.escape_cell("@x") == "'@x"
    assert biblio.unescape_cell("'=SUM(A1)") == "=SUM(A1)" and biblio.unescape_cell("'ok") == "'ok"


# ----- exact dedup --------------------------------------------------------------------------------


def test_exact_duplicates_are_one_work_with_every_provenance_record(lib: Lib) -> None:
    lib.imported()
    bearings = [w for w in lib.works() if w["title"].lower() == "bearing losses in vacuum"]
    assert len(bearings) == 1  # two BibTeX entries and one RIS entry, one DOI
    body = lib.body(bearings[0]["work_id"])
    titles = [r["value"] for r in body["field_provenance"] if r["field"] == "title"]
    assert sorted(titles) == ["Bearing Losses in Vacuum", "Bearing losses in vacuum",
                              "Bearing losses in vacuum"]  # fmt: skip
    assert {r["entry"] for r in body["field_provenance"]} == {
        "bearing2025", "bearing2025b", "bearing2025ris"
    }  # fmt: skip
    assert body["title"] == "Bearing losses in vacuum"  # by the stable key (digest, line)
    keys = [o["value"] for o in body["identifiers"]["other"] if o["scheme"] == "citekey"]
    assert keys == ["bearing2025", "bearing2025b", "bearing2025ris"]  # sorted, deterministic


def test_a_bibliography_entry_fills_the_pdf_work_and_takes_precedence(lib: Lib) -> None:
    lib.imported()
    drag = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/synthetic.2026.001"]
    assert len(drag) == 1 and drag[0]["page_count"] == 4  # the PDF's work, not a second one
    assert drag[0]["year"] == 2026 and drag[0]["venue"] == "Journal of Flywheel Studies"
    body = lib.body(drag[0]["work_id"])
    assert body["title"] == "Aerodynamic Drag of a Flywheel Housing at Low Pressure"  # the entry's
    bases = [(r["field"], r["basis"]) for r in body["field_provenance"]]
    assert ("title", "pdf_info") in bases and ("title", "bibliography") in bases  # both kept
    kers = lib.work("Kinetic energy recovery")
    assert kers["page_count"] == 1 and kers["venue"] == "Synthetic Reviews in Energy"


def test_a_pdf_after_its_bibliography_entry_becomes_that_works_full_text(tmp_path: Path) -> None:
    lib = Lib(tmp_path, pdfs_first=False)
    lib.imported()
    before = lib.work("Aerodynamic")
    assert before["page_count"] is None and before["access_state"] == "metadata_only"
    pdfs.write_folder(lib.folder, {"later/drag.pdf": pdfs.paper(pdfs.DRAG)})
    plan = lib.plan("library/later")
    assert plan["items"][0]["attach"] is True and plan["items"][0]["work_id"] == before["work_id"]
    lib.commit(plan)
    after = lib.work("Aerodynamic")
    assert after["work_id"] == before["work_id"] and after["page_count"] == 4
    assert after["title"] == before["title"] and after["venue"] == before["venue"]
    assert len([w for w in lib.works() if w["title"].startswith("Aerodynamic")]) == 1


def test_resolution_is_deterministic_in_any_file_order(tmp_path: Path) -> None:
    """The same files give the same works whether imported together or one by one."""

    def snapshot(lib: Lib) -> list[tuple[Any, ...]]:
        out = []
        for w in lib.works(OWNER, include_merged=True):
            body = lib.body(w["work_id"])
            out.append((
                body["title"], json.dumps(body["identifiers"], sort_keys=True), body["year"],
                body["venue"], tuple(c["name"] for c in body["contributors"]),
                tuple(sorted((r["field"], r.get("value") or "", r.get("entry") or "")
                             for r in body["field_provenance"])),
            ))  # fmt: skip
        return sorted(out)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    together = Lib(tmp_path / "a")
    together.imported()
    piecewise = Lib(tmp_path / "b")
    for path in ("drag.pdf", "kers.pdf", "refs.bib", "refs.csv", "refs.ris"):
        piecewise.imported(f"library/{path}")
    assert snapshot(together) == snapshot(piecewise)


def test_conflicting_identifiers_are_refused_for_review(lib: Lib) -> None:
    lib.imported()
    seq = lib.seq()
    (lib.folder / "conflict.bib").write_text(
        "@article{both, title={Two works at once}, doi={10.5555/synthetic.2026.020},"
        " pmid={12345678}}\n"
        "@article{two, title={Two DOIs}, doi={10.5555/a 10.5555/b}}\n",
        encoding="utf-8",
    )
    plan = lib.plan("library/conflict.bib")
    found = entries(plan)
    assert found["both"]["action"] == "refused" and "2 different works" in found["both"]["reason"]
    assert found["two"]["action"] == "refused" and "more than one DOI" in found["two"]["reason"]
    assert plan["items"][0]["action"] == "unchanged"  # nothing would be recorded
    lib.commit(plan)
    assert lib.seq() == seq


# ----- similarity candidates and choices ---------------------------------------------------------


def candidate_pair(lib: Lib, kind: str = "duplicate") -> tuple[str, str]:
    found = lib.svc.library_dedup(lib.pid, AGENT)["candidates"]
    (cand,) = [c for c in found if c["kind"] == kind]
    a, b = (w["work_id"] for w in cand["works"])
    return a, b


def test_similar_works_are_only_proposed_and_an_agent_gets_approval_required(lib: Lib) -> None:
    lib.imported()
    result = lib.svc.library_dedup(lib.pid, AGENT)
    (cand,) = result["candidates"]
    assert cand["kind"] == "duplicate"
    assert cand["evidence"] == {"same_title_key": "windage sealed housings survey",
                                "first_author_family": "doe", "years": [2024, 2025]}  # fmt: skip
    a, b = (w["work_id"] for w in cand["works"])
    revs, seq = lib.revs(), lib.seq()
    from prometheos import agentops

    agentops.load()
    ctx = agentops.Ctx(lib.svc, lib.pid, AGENT)
    with pytest.raises(PrometheosError) as asked:
        agentops.execute(agentops.OPS["library_dedup_resolve"], ctx,
                         {"work_a": a, "work_b": b, "choice": "merge"})  # fmt: skip
    assert asked.value.code == "approval_required"
    assert lib.revs() == revs and lib.seq() == seq  # nothing changed
    with pytest.raises(PrometheosError) as owner_only:
        lib.svc.library_dedup_resolve(lib.pid, a, b, "merge", authority=None)
    assert owner_only.value.code == "policy_denied"
    # The owner grants exactly that plan; the request runs once.
    grant(lib.env, asked.value.details["approval_request_id"])
    done = lib.svc.approval_resume(lib.pid, AGENT, asked.value.details["approval_request_id"])
    assert done["result"]["choice"] == "merge"
    assert (
        lib.svc.approval_resume(lib.pid, AGENT, asked.value.details["approval_request_id"])[
            "replayed"
        ]
        is True
    )


def test_merge_sets_merged_into_and_moves_nothing(lib: Lib) -> None:
    lib.imported()
    a, b = candidate_pair(lib)
    before = {w: lib.body(w) for w in (a, b)}
    result = lib.svc.library_dedup_resolve(
        lib.pid, a, b, "merge", "same survey", authority=OWNER_ACT
    )
    merged, into = result["merged"], result["into"]
    assert {merged, into} == {a, b}
    assert lib.body(into) == before[into]  # the surviving work is untouched
    after = lib.body(merged)
    assert after["merged_into"]["id"] == into
    assert {k: v for k, v in after.items() if k != "merged_into"} == {
        k: v for k, v in before[merged].items() if k != "merged_into"
    }
    listed = {w["work_id"] for w in lib.works()}
    assert into in listed and merged not in listed
    assert lib.svc.library_dedup(lib.pid, AGENT)["candidates"] == []
    # Re-importing the merged entry lands on the work that stands for it.
    seq = lib.seq()
    lib.imported()
    assert lib.seq() == seq


def test_a_distinct_choice_is_remembered(lib: Lib) -> None:
    lib.imported()
    a, b = candidate_pair(lib)
    revs = lib.revs()
    lib.svc.library_dedup_resolve(lib.pid, a, b, "distinct", "two surveys", authority=OWNER_ACT)
    assert lib.revs() == revs
    result = lib.svc.library_dedup(lib.pid, AGENT)
    assert result["candidates"] == []
    assert [d["choice"] for d in result["decisions"]] == ["distinct"]
    with pytest.raises(PrometheosError):
        lib.svc.library_dedup_resolve(lib.pid, a, b, "merge", authority=OWNER_ACT)


def test_versions_are_related_never_merged(lib: Lib) -> None:
    lib.imported()
    preprint = next(w for w in lib.works() if w["identifiers"].get("arxiv") == "2603.00042")
    published = next(
        w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/synthetic.2026.010"
    )
    assert preprint["work_id"] != published["work_id"]
    view = lib.svc.library_work(lib.pid, AGENT, published["work_id"])
    assert view["versions"] == [{"work_id": preprint["work_id"], "title": preprint["title"],
                                 "role": "preprint"}]  # fmt: skip
    with closing(sqlite3.connect(lib.env.store_dir / "record.sqlite")) as conn:
        rows = conn.execute(
            "SELECT source_id, target_id FROM relations WHERE relation = 'version_of'"
        ).fetchall()
    assert rows == [(preprint["work_id"], published["work_id"])]  # once, preprint -> published
    # A similar arXiv-only and DOI-only pair is a version candidate, and the choice relates them.
    (lib.folder / "pair.bib").write_text(
        "@misc{p, title={Rotor fatigue under cycling}, author={Ward, K.}, year={2025},"
        " eprint={2504.01111}, archivePrefix={arXiv}}\n"
        "@article{q, title={Rotor Fatigue under Cycling}, author={Ward, Kim}, year={2026},"
        " journal={Invented Fatigue Letters}, doi={10.5555/synthetic.2026.050}}\n",
        encoding="utf-8",
    )
    lib.imported("library/pair.bib")
    a, b = candidate_pair(lib, "version")
    result = lib.svc.library_dedup_resolve(lib.pid, a, b, "versions", authority=OWNER_ACT)
    p_work = next(w for w in lib.works() if w["identifiers"].get("arxiv") == "2504.01111")
    assert result["preprint"] == p_work["work_id"] and result["merged"] is None
    left = lib.svc.library_dedup(lib.pid, AGENT)["candidates"]
    assert [c["kind"] for c in left] == ["duplicate"]  # the version pair is decided


# ----- idempotency and changed files -------------------------------------------------------------


def test_the_same_bytes_create_nothing_and_a_changed_file_revises_only_changed_entries(
    lib: Lib,
) -> None:
    lib.imported()
    seq, revs = lib.seq(), lib.revs()
    again = lib.plan()
    assert again["counts"]["create"] == 0 and again["counts"]["new_version"] == 0
    lib.commit(again)
    assert lib.seq() == seq and lib.revs() == revs
    text = (lib.folder / "refs.bib").read_text(encoding="utf-8")
    changed = text.replace("journal = {Review of Invented Machines}",
                           "journal = {Review of Invented Machines (2nd series)}")  # fmt: skip
    changed = changed.replace("% Synthetic bibliography", "% Edited synthetic bibliography\n%")
    (lib.folder / "refs.bib").write_text(changed, encoding="utf-8")
    plan = lib.plan("library/refs.bib")
    (item,) = plan["items"]
    assert item["action"] == "new_version"
    assert {k: e["action"] for k, e in entries(plan).items() if e["action"] != "unchanged"} == {
        "doe2024survey": "update"
    }
    lib.commit(plan)
    survey = lib.work("Windage in sealed housings: a survey")
    assert survey["venue"] == "Review of Invented Machines (2nd series)"
    after = lib.revs()
    assert [w for w in after if after[w] != revs[w]] == [survey["work_id"]]
    body = lib.body(survey["work_id"])
    venues = [r["value"] for r in body["field_provenance"] if r["field"] == "venue"]
    assert venues == ["Review of Invented Machines (2nd series)"]  # its own records replaced


# ----- export and round trip ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["bibtex", "ris", "csv"])
def test_an_export_round_trips_without_creating_or_changing_anything(lib: Lib, fmt: str) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    hit = lib.svc.library_search(lib.pid, AGENT, "drag coefficient", work_id=drag["work_id"])[
        "hits"
    ][0]
    lib.svc.library_annotate(lib.pid, AGENT, drag["work_id"], page=hit["page"], start=hit["start"],
                             end=hit["end"], text="Compare with the rig.")  # fmt: skip
    out = lib.svc.library_export(lib.pid, AGENT, fmt)
    assert out["count"] == len(lib.works())
    assert len({w["key"] for w in out["works"]}) == out["count"]  # stable, unique keys
    assert "Compare with the rig." in out["content"]
    assert out == lib.svc.library_export(lib.pid, AGENT, fmt) | {"project_seq": out["project_seq"]}
    suffix = {"bibtex": "bib", "ris": "ris", "csv": "csv"}[fmt]
    target = lib.env.research / "exports" / f"all.{suffix}"
    target.parent.mkdir()
    target.write_text(out["content"], encoding="utf-8", newline="")
    seq, revs = lib.seq(), lib.revs()
    plan = lib.plan("exports")
    found = entries(plan)
    assert found and {e["action"] for e in found.values()} == {"unchanged"}
    lib.commit(plan)
    assert lib.seq() == seq and lib.revs() == revs


# ----- annotations --------------------------------------------------------------------------------


def test_an_annotation_cites_a_verified_pdf_span(lib: Lib) -> None:
    lib.imported()
    drag = lib.work("Aerodynamic")
    hit = lib.svc.library_search(lib.pid, AGENT, "0.0042", work_id=drag["work_id"])["hits"][0]
    span = {"page": hit["page"], "start": hit["start"], "end": hit["end"]}
    made = lib.svc.library_annotate(
        lib.pid, AGENT, drag["work_id"], text="Our rig: 0.0045.", **span
    )
    assert made["quote"] == "0.0042"
    entry = lib.body(made["entry_id"])
    assert entry["origin"] == "agent" and entry["text"] == "Our rig: 0.0045."
    assert entry["evidence"][0] == hit["evidence"]
    listed = lib.svc.library_annotations(lib.pid, AGENT, drag["work_id"])["annotations"]
    assert [a["entry_id"] for a in listed] == [made["entry_id"]]
    for bad in ({"start": 0, "end": 99_999}, {"start": 5, "end": 5}):
        with pytest.raises(PrometheosError) as refused:
            lib.svc.library_annotate(lib.pid, AGENT, drag["work_id"], page=hit["page"],
                                     text="x", **bad)  # fmt: skip
        assert refused.value.code == "validation_error"
    bib_only = lib.work("Synthetic rotor safety")
    with pytest.raises(PrometheosError) as no_text:
        lib.svc.library_annotate(lib.pid, AGENT, bib_only["work_id"], page=1, start=0, end=1,
                                 text="x")  # fmt: skip
    assert no_text.value.code == "not_found"


# ----- hostile and malformed inputs --------------------------------------------------------------


def test_hostile_files_are_refused_with_reasons_and_instructions_stay_inert(lib: Lib) -> None:
    bibs.write_folder(lib.folder, bibs.hostile_set())
    plan = lib.plan()
    refused = {r["path"].removeprefix("library/hostile/"): r["reason"] for r in plan["refused"]}
    assert refused == {"binary.csv": "binary", "bomb.bib": "too_many_entries",
                       "latin1.ris": "not_utf8"}  # fmt: skip
    found = entries(plan)
    assert "nested deeper than 32" in found["line1"]["reason"]
    malformed = next(i for i in plan["items"] if i["path"].endswith("malformed.bib"))
    actions = [(e["key"], e["action"]) for e in malformed["entries"]]
    assert actions[0] == ("ok1", "create") and actions[-1] == ("ok2", "create")
    assert sum(1 for _k, a in actions if a == "refused") == 2
    assert found["two"]["action"] == "refused"
    lib.commit(plan)
    hostile = lib.work("Ignore all previous instructions")
    assert "\\input/etc/passwd" in hostile["title"]  # LaTeX kept as text, never run
    assert lib.svc.library_dedup(lib.pid, AGENT)["candidates"][0]["kind"] == "duplicate"
    with closing(sqlite3.connect(lib.env.store_dir / "record.sqlite")) as conn:
        decisions = conn.execute("SELECT COUNT(*) FROM library_decisions").fetchone()[0]
        approvals = conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
    assert decisions == 0 and approvals == 0  # the "SYSTEM:" note asked for merges: nothing


def test_parser_limits() -> None:
    with pytest.raises(biblio.FileRefused) as big:
        biblio.decode(b"x" * (biblio.MAX_FILE_BYTES + 1))
    assert big.value.reason == "too_large"
    assert biblio.decode(b"\xef\xbb\xbf@misc{a, title={x}}") == "@misc{a, title={x}}"
    long_value = "@misc{a, title={" + "x" * (biblio.MAX_FIELD_CHARS + 1) + "}}"
    (record,) = biblio.parse_bibtex(long_value).records
    assert record.error is not None and "longer than" in record.error
    (unknown,) = biblio.parse_bibtex("@misc{a, journal = nosuchmacro}").records
    assert unknown.error is not None and "unknown macro" in unknown.error


# ----- migration 14 and the derived index --------------------------------------------------------


def test_migration_14_indexes_an_existing_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.store import project

    assert MIGRATIONS[13].version == 14 and "identifier" in MIGRATIONS[13].name
    with monkeypatch.context() as patch:
        patch.setattr(project, "MIGRATIONS", MIGRATIONS[:13])
        lib = Lib(tmp_path)
        lib.imported("library/drag.pdf")
    assert lib.svc.migrate(lib.pid, authority=OWNER_ACT)["applied"] == list(
        range(14, len(MIGRATIONS) + 1)
    )
    with closing(sqlite3.connect(lib.env.store_dir / "record.sqlite")) as conn:
        rows = conn.execute(
            "SELECT scheme, value FROM library_identifiers ORDER BY scheme"
        ).fetchall()
    # One identity per PDF (review F1): the DOI; the arXiv id names the preprint.
    assert rows == [("doi", "10.5555/synthetic.2026.001"), ("version_arxiv", "2601.01234")]
    plan = lib.plan("library/refs.bib")
    assert entries(plan)["drag2026"]["action"] == "merge"


def test_the_identifier_index_is_rebuilt_from_records(lib: Lib) -> None:
    lib.imported()
    db = lib.env.store_dir / "record.sqlite"
    with closing(sqlite3.connect(db)) as conn:
        before = sorted(conn.execute("SELECT * FROM library_identifiers").fetchall())
        entries_before = sorted(conn.execute("SELECT * FROM library_entries").fetchall())
        conn.execute("DELETE FROM library_identifiers")
        conn.execute("DELETE FROM library_entries")
        conn.commit()
        from prometheos.store import derived

        derived.rebuild(conn)
        conn.commit()
        assert sorted(conn.execute("SELECT * FROM library_identifiers").fetchall()) == before
        assert sorted(conn.execute("SELECT * FROM library_entries").fetchall()) == entries_before


def test_title_keys_and_families() -> None:
    assert dedup.title_key("Windage in {S}ealed Housings -- A Survey") == dedup.title_key(
        "Windage in sealed housings: a survey"
    )
    assert dedup.title_key(r"Caf\'e d\"{u} r\'{e}sum\'e") == dedup.title_key("Cafe du resume")
    assert dedup.family([{"name": "Doe, Jane", "role": "author"}]) == "doe"
    assert dedup.family([{"name": "Jane Doe", "role": "author"}]) == "doe"


# ----- the CLI ------------------------------------------------------------------------------------


def test_cli_imports_lists_candidates_and_an_agent_resolve_asks_for_approval(
    tmp_path: Path,
) -> None:
    from test_cp1_cli import Cli

    lib = Lib(tmp_path)
    cli = Cli(tmp_path)
    base = ("--project", lib.pid)
    code, plan, err = cli("library", "import", "--plan", "--root", "research", "--path",
                          "library", *base)  # fmt: skip
    assert code == 0, err
    code, _done, err = cli("library", "import", "--commit", plan["plan_id"], *base)
    assert code == 0, err
    code, found, _ = cli("library", "dedup", "list", *base)
    assert code == 0 and found["total"] == 1
    a, b = (w["work_id"] for w in found["candidates"][0]["works"])
    code, asked, _ = cli("library", "dedup", "resolve", a, b, "--distinct", *base)
    assert code == 4 and asked["error"]["code"] == "approval_required"
    out = tmp_path / "out.ris"
    code, written, _ = cli("library", "export", "--format", "ris", "--out", str(out), *base)
    assert code == 0 and out.read_text(encoding="utf-8").startswith("TY  - ")
    assert "content" not in written


def test_the_stable_key_decides_and_every_value_stays(lib: Lib) -> None:
    """The recorded precedence: bibliography over PDF-derived; among bibliography records the
    lower file digest, then the earlier line; every asserted value stays in the provenance."""
    (lib.folder / "order").mkdir()
    (lib.folder / "order" / "a.bib").write_text(
        "@article{first, title={First title}, journal={Venue One}, doi={10.5555/order.1}}\n"
        "@article{second, title={Second title}, journal={Venue Two}, doi={10.5555/order.1},"
        " year={2020}}\n",
        encoding="utf-8",
    )
    lib.imported("library/order")
    (work,) = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/order.1"]
    assert (work["title"], work["venue"], work["year"]) == ("First title", "Venue One", 2020)
    body = lib.body(work["work_id"])
    assert [r["value"] for r in body["field_provenance"] if r["field"] == "title"] == [
        "First title", "Second title"
    ]  # fmt: skip
    lib.imported()
    drag = lib.body(lib.work("Aerodynamic")["work_id"])
    titles = {r["basis"]: r["value"] for r in drag["field_provenance"] if r["field"] == "title"}
    assert titles == {
        "pdf_info": "Aerodynamic drag of a flywheel housing at low pressure",
        "bibliography": "Aerodynamic Drag of a Flywheel Housing at Low Pressure",
    }
    assert drag["title"] == titles["bibliography"]  # the explicit field beats the PDF's


# ----- review round 1 (review-precheck-s11-2-a) regressions ---------------------------------------

ORDER_FILES = {
    "a.bib": (
        "@article{alpha, title={Alpha study of things}, author={Smith, John and Lee, K.},"
        " journal={Journal of Things}, year={2020}, doi={10.5555/order.alpha},"
        " abstract={Alpha abstract as BibTeX words it.}}\n"
        "@article{beta, title={Beta only in BibTeX}, author={Roe, A.}, year={2019},"
        " doi={10.5555/order.beta}}\n"
        "@misc{gamma, title={Gamma preprint}, author={Poe, G.}, year={2021},"
        " eprint={2101.00001v1}, archivePrefix={arXiv}}\n"
    ),
    "b.ris": (
        "TY  - JOUR\nID  - alpha-ris\nTI  - alpha study of things.\nAU  - J. X\nPY  - 2021\n"
        "JO  - J. Things\nDO  - 10.5555/order.alpha\nAB  - Alpha abstract as RIS words it.\n"
        "ER  - \n"
    ),
    "c.csv": "key,title,authors,year,journal,doi,arxiv,abstract\n"
    "alpha-csv,ALPHA STUDY OF THINGS,Smith J.,2020,Things Journal,10.5555/order.alpha,,"
    "Alpha abstract as CSV words it.\n"
    "gamma-csv,Gamma preprint (v2),Poe G.,2022,,,arXiv:2101.00001v2,\n",
}


def normalised(lib: Lib) -> list[str]:
    """Every work's whole body and summary, with record IDs replaced by what they name (a
    version by its file digest, a source by its file's digest, a work by its title): nothing
    is left out, so any order-dependent field or ``other[]`` entry shows (review R2-F1)."""
    names: dict[str, str] = {}
    works = lib.works(OWNER, include_merged=True)
    bodies = {w["work_id"]: lib.body(w["work_id"]) for w in works}
    for work_id, body in bodies.items():
        names[work_id] = "work:" + body["title"]

    for version_id in re.findall(r"version_[0-9a-f-]{36}", json.dumps(bodies)):
        version = lib.body(version_id)
        names[version_id] = "version:" + version["sha256"]
        names[version["source_id"]] = "source:" + version["sha256"]

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str) and value.startswith(("version_", "source_")):
            return names.get(value, "source:?")  # a source no cited version names: a defect
        return names.get(value, value) if isinstance(value, str) else value

    out = [
        json.dumps({"summary": walk(w), "body": walk(bodies[w["work_id"]])}, sort_keys=True)
        for w in works
    ]
    assert "source:?" not in "".join(out)
    return sorted(out)


def test_f0_every_import_order_gives_the_same_works(tmp_path: Path) -> None:
    from itertools import permutations

    results = []
    for n, order in enumerate(permutations(ORDER_FILES)):
        folder = tmp_path / f"o{n}"
        folder.mkdir()
        lib = Lib(folder, pdfs_first=False)
        bibs.write_folder(lib.folder / "order", {k: v.encode() for k, v in ORDER_FILES.items()})
        for name in order:
            lib.imported(f"library/order/{name}")
        alpha = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/order.alpha"]
        assert len(alpha) == 1
        gamma = [w for w in lib.works() if w["identifiers"].get("arxiv") == "2101.00001"]
        assert len(gamma) == 1
        results.append(normalised(lib))
    assert all(r == results[0] for r in results[1:])
    assert len(results) == 6
    # The choices are the stable key's, whichever file came last.
    works = {json.loads(w)["body"]["title"]: json.loads(w)["body"] for w in results[0]}
    shas = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in ORDER_FILES.items()}
    lowest = min(shas, key=shas.__getitem__)
    alpha = works[next(t for t in works if t.lower().startswith("alpha"))]
    ref = alpha["abstract_ref"]
    assert ref["version_id"] == "version:" + shas[lowest]
    assert ref["source_id"] == "source:" + shas[lowest]
    abstracts = [r for r in alpha["field_provenance"] if r["field"] == "abstract_ref"]
    assert len(abstracts) == 3  # every abstract stays as a record
    gamma = works[next(t for t in works if t.startswith("Gamma"))]
    bib_or_csv = min(("a.bib", "c.csv"), key=shas.__getitem__)
    version = {"a.bib": "v1", "c.csv": "v2"}[bib_or_csv]
    assert gamma["identifiers"]["arxiv"] == "2101.00001"
    assert [o for o in gamma["identifiers"]["other"] if o["scheme"] == "arxiv_version"] == [
        {"scheme": "arxiv_version", "value": version}
    ]
    citekeys = [o["value"] for o in gamma["identifiers"]["other"] if o["scheme"] == "citekey"]
    assert citekeys == ["gamma", "gamma-csv"]


def test_f1_a_pdf_with_a_doi_and_an_arxiv_id_is_the_published_work_in_either_order(
    tmp_path: Path,
) -> None:
    pair = (
        "@misc{pre, title={Aerodynamic drag of a flywheel housing (preprint)},"
        " author={Synthetic, A.}, year={2025}, eprint={2601.01234}, archivePrefix={arXiv}}\n"
        "@article{pub, title={Aerodynamic drag of a flywheel housing at low pressure},"
        " author={Synthetic, A.}, journal={Journal of Flywheel Studies}, year={2026},"
        " doi={10.5555/synthetic.2026.001}}\n"
    )
    outcomes = []
    for name, pdf_first in (("pdf-first", True), ("bib-first", False)):
        folder = tmp_path / name
        folder.mkdir()
        lib = Lib(folder, pdfs_first=False)
        steps = [("library/p", {"p/drag.pdf": pdfs.paper(pdfs.DRAG)}),
                 ("library/b", {"b/pair.bib": pair.encode()})]  # fmt: skip
        for path, files in steps if pdf_first else reversed(steps):
            bibs.write_folder(lib.folder, files)
            lib.imported(path)
        works = lib.works()
        published = [
            w for w in works if w["identifiers"].get("doi") == "10.5555/synthetic.2026.001"
        ]
        preprint = [w for w in works if w["identifiers"].get("arxiv") == "2601.01234"]
        assert len(published) == 1 and len(preprint) == 1, name
        assert published[0]["work_id"] != preprint[0]["work_id"]  # related, never merged
        assert published[0]["page_count"] == 4  # the PDF is the published work's full text
        view = lib.svc.library_work(lib.pid, AGENT, published[0]["work_id"])
        assert [(v["work_id"], v["role"]) for v in view["versions"]] == [
            (preprint[0]["work_id"], "preprint")
        ]
        outcomes.append((len(works), published[0]["title"], preprint[0]["title"]))
    assert outcomes[0] == outcomes[1]


def test_f2_an_agent_cannot_add_version_of_and_the_owner_can_undo_a_link(lib: Lib) -> None:
    lib.imported()
    a, b = candidate_pair(lib)
    seq = lib.seq()
    op = {"op": "relation_add", "source_ref": {"id": a, "rev": 1},
          "target_ref": {"id": b, "rev": 1}, "relation": "version_of"}  # fmt: skip
    with pytest.raises(PrometheosError) as refused:
        lib.env.apply([op], caller=AGENT)
    assert refused.value.code == "policy_denied" and "version_of" in refused.value.message
    assert lib.seq() == seq and candidate_pair(lib) == (a, b)  # the candidate stays
    # An import's link, undone by the owner, stays undone; an agent only asks.
    preprint = next(w for w in lib.works() if w["identifiers"].get("arxiv") == "2603.00042")
    published = next(
        w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/synthetic.2026.010"
    )
    with pytest.raises(PrometheosError) as asked:
        from prometheos import agentops

        agentops.load()
        ctx = agentops.Ctx(lib.svc, lib.pid, AGENT)
        pair_ids = {"work_a": preprint["work_id"], "work_b": published["work_id"]}
        agentops.execute(agentops.OPS["library_dedup_undo"], ctx, pair_ids)
    assert asked.value.code == "approval_required"
    undone = lib.svc.library_dedup_undo(lib.pid, preprint["work_id"], published["work_id"],
                                        "wrong link", authority=OWNER_ACT)  # fmt: skip
    assert undone["undone"] == "link"
    assert lib.svc.library_work(lib.pid, AGENT, published["work_id"])["versions"] == []
    text = (lib.folder / "refs.bib").read_text(encoding="utf-8")
    (lib.folder / "refs.bib").write_text(text + "\n% edited\n", encoding="utf-8")
    lib.imported("library/refs.bib")
    assert lib.svc.library_work(lib.pid, AGENT, published["work_id"])["versions"] == []
    # A merge is undone too: the merged work is listed again and the pair proposed again.
    result = lib.svc.library_dedup_resolve(lib.pid, a, b, "merge", authority=OWNER_ACT)
    lib.svc.library_dedup_undo(lib.pid, a, b, authority=OWNER_ACT)
    assert lib.body(result["merged"])["merged_into"] is None
    assert result["merged"] in {w["work_id"] for w in lib.works()}
    assert candidate_pair(lib) == (a, b)


def test_f3_an_overlong_field_or_a_year_out_of_range_refuses_that_field_not_the_plan(
    lib: Lib,
) -> None:
    long = "x" * 301
    (lib.folder / "bad.bib").write_text(
        f"@article{{longtitle, title={{{long}}}, doi={{10.5555/bad.1}}}}\n"
        f"@article{{longvenue, title={{Fine title}}, journal={{{long}}}, year={{2500}}}}\n"
        "@article{good, title={A good entry}, year={2020}}\n",
        encoding="utf-8",
    )
    plan = lib.plan("library/bad.bib")
    found = entries(plan)
    assert found["good"]["action"] == "create"
    assert found["longtitle"]["action"] == "refused" and "title" in found["longtitle"]["reason"]
    assert {f["field"] for f in found["longtitle"]["refused_fields"]} == {"title"}
    assert found["longvenue"]["action"] == "create"
    assert {f["field"]: f["reason"] for f in found["longvenue"]["refused_fields"]} == {
        "venue": "longer than 300 characters", "year": "a year is 1000 to 2200"
    }  # fmt: skip
    lib.commit(plan)
    assert lib.work("A good entry")["year"] == 2020
    assert lib.work("Fine title")["year"] is None


def test_f4_the_same_bytes_at_another_path_create_nothing(lib: Lib) -> None:
    lib.imported()
    seq, revs = lib.seq(), lib.revs()
    (lib.folder / "copy").mkdir()
    (lib.folder / "copy" / "refs_copy.bib").write_bytes((lib.folder / "refs.bib").read_bytes())
    plan = lib.plan("library/copy")
    (item,) = plan["items"]
    assert item["action"] == "unchanged" and item["same_bytes_as"] == "library/refs.bib"
    lib.commit(plan)
    assert lib.seq() == seq and lib.revs() == revs


def test_f5_purge_removes_the_decisions_about_purged_works(lib: Lib) -> None:
    lib.imported()
    a, b = candidate_pair(lib)
    lib.svc.library_dedup_resolve(lib.pid, a, b, "distinct", "my private note: DO NOT SHARE",
                                  authority=OWNER_ACT)  # fmt: skip
    plan = lib.svc.purge_plan(lib.pid, [a], OWNER)
    secret = lib.svc.grant(
        lib.pid, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC"
    )
    lib.svc.purge_commit(lib.pid, plan["plan_id"], secret.secret)
    with closing(sqlite3.connect(lib.env.store_dir / "record.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM library_decisions").fetchone()[0] == 0
    for caller in (AGENT, OWNER):
        assert lib.svc.library_dedup(lib.pid, caller)["decisions"] == []
    dump = json.dumps(lib.svc.library_dedup(lib.pid, OWNER))
    assert "DO NOT SHARE" not in dump and a not in dump


def test_f6_macro_repetition_is_refused_with_bounded_memory() -> None:
    import tracemalloc

    text = (
        "@string{a = {"
        + "y" * 19_000
        + "}}\n@misc{bomb, title = "
        + " # ".join(["a"] * 20_000)
        + "}\n@misc{fine, title={Still read}}\n"
    )
    tracemalloc.start()
    try:
        parsed = biblio.parse_bibtex(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024, peak
    bomb, fine = parsed.records
    assert bomb.error is not None and "longer than" in bomb.error
    assert fine.fields["title"].value == "Still read"
    with pytest.raises(biblio.FileRefused) as expanded:
        many = (
            "@string{a = {"
            + "y" * 15_000
            + "}}\n"
            + "".join(f"@misc{{k{i}, title = a}}\n" for i in range(2300))
        )
        biblio.parse_bibtex(many)
    assert "macro expansion" in expanded.value.detail


def test_low_bom_abstract_digest_checks_out(lib: Lib) -> None:
    from cp1_support import create_entry

    (lib.folder / "bom.bib").write_bytes(
        b"\xef\xbb\xbf@article{one, title={One line}, abstract={A one-line abstract.}}\n"
    )
    lib.imported("library/bom.bib")
    work = lib.body(lib.work("One line")["work_id"])
    assert lib.env.apply([create_entry(title="Cites", evidence=[work["abstract_ref"]])])["ok"]


def test_low_export_edge_values_round_trip(lib: Lib) -> None:
    backslash = chr(92)
    (lib.folder / "edge.ris").write_text(
        f"TY  - JOUR{chr(10)}ID  - path{chr(10)}TI  - Path to C:{backslash}{chr(10)}"
        f"AU  - Doe, J.{chr(10)}PY  - 2020{chr(10)}DO  - 10.5555/edge.1{chr(10)}ER  - {chr(10)}"
        f"TY  - JOUR{chr(10)}ID  - omics{chr(10)}TI  - '-omics approaches{chr(10)}"
        f"AU  - Roe, A.{chr(10)}PY  - 2021{chr(10)}DO  - 10.5555/edge.2{chr(10)}ER  - {chr(10)}",
        encoding="utf-8",
    )
    lib.imported("library/edge.ris")
    assert lib.work("Path to C:")["title"] == "Path to C:" + backslash
    assert lib.work("'-omics")["title"] == "'-omics approaches"  # a leading apostrophe kept
    for fmt, suffix in (("bibtex", "bib"), ("csv", "csv")):
        out = lib.svc.library_export(lib.pid, AGENT, fmt)["content"]
        target = lib.env.research / f"exports-{fmt}" / f"all.{suffix}"
        target.parent.mkdir()
        target.write_text(out, encoding="utf-8", newline="")
        seq, revs = lib.seq(), lib.revs()
        plan = lib.plan(f"exports-{fmt}")
        assert {e["action"] for e in entries(plan).values()} == {"unchanged"}, fmt
        lib.commit(plan)
        assert lib.seq() == seq and lib.revs() == revs


def test_low_wiley_dois_and_a_labelled_pmid(lib: Lib) -> None:
    sici = "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2"
    assert ids.doi(sici) == sici.lower()
    assert ids.doi("https://www.doi.org/10.5555/a%2Fb?utm_source=x") == "10.5555/a/b"
    with pytest.raises(ids.Invalid) as two:
        ids.doi("10.5555/a; 10.5555/b")
    assert "more than one" in two.value.reason
    (record,) = biblio.parse_ris("TY  - JOUR\nTI  - Not PubMed\nAN  - 42\nER  - \n").records
    assert "pmid" not in record.identity  # an unlabelled accession number is not a PMID


def test_low_restricted_works_are_annotated_at_their_sensitivity(lib: Lib) -> None:
    lib.commit(lib.plan("library/drag.pdf", sensitivity="restricted"))
    work = next(w for w in lib.works(OWNER) if w["title"].startswith("Aerodynamic"))
    hit = lib.svc.library_search(lib.pid, OWNER, "drag coefficient", work_id=work["work_id"])[
        "hits"
    ][0]
    span = {"page": hit["page"], "start": hit["start"], "end": hit["end"]}
    made = lib.svc.library_annotate(
        lib.pid, OWNER, work["work_id"], text="Restricted note.", **span
    )
    assert lib.record(made["entry_id"])["sensitivity"] == "restricted"
    with pytest.raises(PrometheosError):
        lib.svc.library_annotations(lib.pid, AGENT, work["work_id"])


def test_low_a_foreign_export_resolves_by_identifiers(lib: Lib, tmp_path: Path) -> None:
    lib.imported("library/refs.bib")
    out = lib.svc.library_export(lib.pid, AGENT, "bibtex")["content"]
    (tmp_path / "other").mkdir()
    other = Lib(tmp_path / "other", pdfs_first=False)
    (other.folder / "foreign").mkdir()
    (other.folder / "foreign" / "from-elsewhere.bib").write_text(out, encoding="utf-8")
    plan = other.plan("library/foreign")
    actions = {e["key"]: e["action"] for e in entries(plan).values()}
    assert set(actions.values()) <= {"create", "merge"} and "refused" not in actions.values()


# ----- mutants the review found surviving -------------------------------------------------------


def test_mutant_an_entry_contradicting_the_matched_work_is_refused(lib: Lib) -> None:
    (lib.folder / "w.bib").write_text(
        "@article{w, title={Both identifiers}, doi={10.5555/both.1}, pmid={111}}" + chr(10),
        encoding="utf-8",
    )
    lib.imported("library/w.bib")
    (lib.folder / "contra.bib").write_text(
        "@article{c, title={Same DOI, other PMID}, doi={10.5555/both.1}, pmid={222}}" + chr(10),
        encoding="utf-8",
    )
    found = entries(lib.plan("library/contra.bib"))
    assert found["c"]["action"] == "refused"
    assert "the matched work has another pmid" in found["c"]["reason"]


def test_mutant_merge_keeps_the_older_work(lib: Lib) -> None:
    lib.imported()
    a, b = candidate_pair(lib)
    older = next(w for w in lib.works() if w["title"] == "Windage in sealed housings: a survey")
    result = lib.svc.library_dedup_resolve(lib.pid, b, a, "merge", authority=OWNER_ACT)
    assert result["into"] == older["work_id"] and result["merged"] != older["work_id"]


def test_mutant_candidates_need_close_years_and_the_same_first_author(lib: Lib) -> None:
    (lib.folder / "near.bib").write_text(
        "@article{y1, title={Gyroscopic loads in rotors}, author={Ng, A.}, year={2020}}\n"
        "@article{y2, title={Gyroscopic Loads in Rotors}, author={Ng, A.}, year={2022}}\n"
        "@article{a1, title={Seal wear under vacuum}, author={Ng, A.}, year={2020}}\n"
        "@article{a2, title={Seal Wear under Vacuum}, author={Ode, B.}, year={2020}}\n"
        "@article{k1, title={Hub fatigue}, author={Ng, A.}, year={2020}}\n"
        "@article{k2, title={Hub Fatigue}, author={Ng, A.}, year={2021}}\n",
        encoding="utf-8",
    )
    lib.imported("library/near.bib")
    keys = {c["title_key"] for c in lib.svc.library_dedup(lib.pid, AGENT)["candidates"]}
    assert "hub fatigue" in keys  # one year apart: proposed
    assert "gyroscopic loads rotors" not in keys  # two years apart: not proposed
    assert "seal wear under vacuum" not in keys  # another first author: not proposed


def test_mutant_export_keys_are_unique(lib: Lib) -> None:
    (lib.folder / "keys.bib").write_text(
        "@article{same, title={First with key}, doi={10.5555/key.1}}\n", encoding="utf-8"
    )
    (lib.folder / "keys.ris").write_text(
        "TY  - JOUR\nID  - same\nTI  - Second with key\nDO  - 10.5555/key.2\nER  - \n",
        encoding="utf-8",
    )
    lib.imported("library/keys.bib")
    lib.imported("library/keys.ris")
    out = lib.svc.library_export(lib.pid, AGENT, "bibtex")
    keys = [w["key"] for w in out["works"]]
    assert len(keys) == len(set(keys))
    assert {k for k in keys if k.startswith("same")} == {"same", "samea"}


def test_f0_the_file_digest_decides_before_the_line(lib: Lib) -> None:
    """Of two files asserting a title for one DOI, the file with the lower SHA-256 wins, even
    when its entry is further down: the stable key is (basis, digest, line), not the line."""
    import hashlib

    def entry(title: str, padded: bool) -> bytes:
        pad = "%" + chr(10)
        line = "@article{k, title={" + title + "}, doi={10.5555/key.x}}" + chr(10)
        return ((pad * 4 if padded else "") + line).encode()

    for padded in ("x", "y"):
        x, y = entry("From x", padded == "x"), entry("From y", padded == "y")
        low = "x" if hashlib.sha256(x).hexdigest() < hashlib.sha256(y).hexdigest() else "y"
        if low == padded:
            break
    else:  # pragma: no cover - one of the two arrangements always works
        raise AssertionError("no arrangement puts the lower digest on the later line")
    (lib.folder / "digest").mkdir()
    (lib.folder / "digest" / "x.bib").write_bytes(x)
    (lib.folder / "digest" / "y.bib").write_bytes(y)
    lib.imported("library/digest")
    (work,) = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/key.x"]
    assert work["title"] == f"From {low}"


# ----- review round 2 (review-precheck-s11-2-a-r2) regressions ----------------------------------


def _edit_storage_entries(lib: Lib, n: int) -> None:
    """Change both storage entries of refs.bib (the published venue, the preprint's version),
    so that a re-import writes both works and links them again unless a choice forbids it."""
    text = bibs.REFS_BIB
    text = text.replace(
        "journal = {Transport Energy Letters}",
        "journal = {Transport Energy Letters " + str(n) + "}",
    )
    text = text.replace("eprint = {2603.00042v2}", "eprint = {2603.00042v" + str(n + 2) + "}")
    assert text != bibs.REFS_BIB
    (lib.folder / "refs.bib").write_text(text, encoding="utf-8")


def _storage_pair(lib: Lib) -> tuple[str, str]:
    preprint = next(w for w in lib.works() if w["identifiers"].get("arxiv") == "2603.00042")
    published = next(
        w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/synthetic.2026.010"
    )
    return preprint["work_id"], published["work_id"]


def test_r2_f2_an_undone_import_link_can_be_linked_again_or_forgotten(lib: Lib) -> None:
    lib.imported()
    preprint, published = _storage_pair(lib)

    def links() -> list[tuple[str, str, str]]:
        found = lib.svc.library_dedup(lib.pid, AGENT)["links"]
        return [(x["preprint"], x["published"], x["origin"]) for x in found]

    def choices() -> list[str]:
        return [d["choice"] for d in lib.svc.library_dedup(lib.pid, AGENT)["decisions"]]

    assert links() == [(preprint, published, "import")]
    undone = lib.svc.library_dedup_undo(lib.pid, preprint, published, authority=OWNER_ACT)
    assert undone["undone"] == "link" and links() == [] and choices() == ["unlinked"]
    # A re-import that rewrites both works does not link them again (R2-F4: the marker is
    # recorded, and link_versions honours it).
    revs = lib.revs()
    _edit_storage_entries(lib, 1)
    lib.imported("library/refs.bib")
    after = lib.revs()
    assert after[preprint] > revs[preprint] and after[published] > revs[published]
    assert links() == [] and choices() == ["unlinked"]
    # The marker can be forgotten: then a later import links the pair again.
    forgot = lib.svc.library_dedup_undo(lib.pid, preprint, published, authority=OWNER_ACT)
    assert forgot["undone"] == "unlinked" and choices() == [] and links() == []
    _edit_storage_entries(lib, 2)
    lib.imported("library/refs.bib")
    assert links() == [(preprint, published, "import")]
    # Or the owner links the pair again at once, as an owner choice.
    lib.svc.library_dedup_undo(lib.pid, preprint, published, authority=OWNER_ACT)
    relinked = lib.svc.library_dedup_resolve(lib.pid, preprint, published, "versions",
                                             authority=OWNER_ACT)  # fmt: skip
    assert (relinked["preprint"], relinked["published"]) == (preprint, published)
    assert links() == [(preprint, published, "decision")] and choices() == ["versions"]
    view = lib.svc.library_work(lib.pid, AGENT, published)
    assert [v["work_id"] for v in view["versions"]] == [preprint]
    # An agent asks for the same through an approval.
    from prometheos import agentops

    agentops.load()
    ctx = agentops.Ctx(lib.svc, lib.pid, AGENT)
    with pytest.raises(PrometheosError) as asked:
        agentops.execute(agentops.OPS["library_dedup_undo"], ctx,
                         {"work_a": preprint, "work_b": published})  # fmt: skip
    assert asked.value.code == "approval_required"


def test_r2_f3_undoing_a_merge_never_leaves_two_works_with_one_identifier(lib: Lib) -> None:
    (lib.folder / "f3").mkdir()
    (lib.folder / "f3" / "x.bib").write_text(
        "@article{x, title={Flux pinning in flywheel rotors}, author={Ward, K.}, year={2025}}\n",
        encoding="utf-8",
    )
    lib.imported("library/f3/x.bib")
    (lib.folder / "f3" / "m.bib").write_text(
        "@article{m, title={Flux Pinning in Flywheel Rotors}, author={Ward, Kim}, year={2025},"
        " doi={10.5555/f3.1}}\n",
        encoding="utf-8",
    )
    lib.imported("library/f3/m.bib")
    a, b = candidate_pair(lib)
    merge = lib.svc.library_dedup_resolve(lib.pid, a, b, "merge", authority=OWNER_ACT)
    into, merged = merge["into"], merge["merged"]
    assert lib.body(merged)["identifiers"]["doi"] == "10.5555/f3.1"
    assert lib.body(into)["identifiers"]["doi"] is None
    # A later import with the merged work's DOI lands on the work that stands for it.
    (lib.folder / "f3" / "later.bib").write_text(
        "@article{later, title={Flux pinning in flywheel rotors}, journal={Invented Letters},"
        " doi={10.5555/f3.1}}\n",
        encoding="utf-8",
    )
    lib.imported("library/f3/later.bib")
    assert lib.body(into)["identifiers"]["doi"] == "10.5555/f3.1"
    seq = lib.seq()
    with pytest.raises(PrometheosError) as refused:
        lib.svc.library_dedup_undo(lib.pid, a, b, authority=OWNER_ACT)
    assert refused.value.code == "validation_error"
    assert "two works with one identifier" in refused.value.message
    assert into in refused.value.message and "doi" in refused.value.message
    assert lib.seq() == seq and lib.body(merged)["merged_into"]["id"] == into
    owners = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/f3.1"]
    assert [w["work_id"] for w in owners] == [into]


def test_r2_f4_undo_is_owner_only_at_the_service(lib: Lib) -> None:
    lib.imported()
    preprint, published = _storage_pair(lib)
    seq = lib.seq()
    for authority in (None, AGENT, "owner"):
        with pytest.raises(PrometheosError) as denied:
            lib.svc.library_dedup_undo(lib.pid, preprint, published, authority=authority)
        assert denied.value.code == "policy_denied"
    assert lib.seq() == seq
    assert lib.svc.library_dedup(lib.pid, AGENT)["links"][0]["origin"] == "import"


def test_r2_f4_works_with_different_identifiers_are_never_merged(lib: Lib) -> None:
    (lib.folder / "f4.bib").write_text(
        "@article{p, title={Bearing wear in vacuum}, author={Ward, K.}, year={2025},"
        " doi={10.5555/f4.1}}\n"
        "@article{q, title={Bearing Wear in Vacuum}, author={Ward, Kim}, year={2025},"
        " doi={10.5555/f4.2}}\n",
        encoding="utf-8",
    )
    lib.imported("library/f4.bib")
    p, q = (
        next(w["work_id"] for w in lib.works() if w["identifiers"].get("doi") == doi)
        for doi in ("10.5555/f4.1", "10.5555/f4.2")
    )
    assert lib.svc.library_dedup(lib.pid, AGENT)["candidates"] == []  # never even proposed
    seq = lib.seq()
    with pytest.raises(PrometheosError) as refused:
        lib.svc.library_dedup_resolve(lib.pid, p, q, "merge", authority=OWNER_ACT)
    assert refused.value.code == "validation_error"
    assert "different doi identifiers" in refused.value.message
    assert lib.seq() == seq
    assert lib.body(p)["merged_into"] is None and lib.body(q)["merged_into"] is None
    # Keeping them separate stays possible.
    kept = lib.svc.library_dedup_resolve(lib.pid, p, q, "distinct", authority=OWNER_ACT)
    assert kept["choice"] == "distinct"


def test_r2_f5_subject_classes_and_trailing_punctuation_are_normalised(lib: Lib) -> None:
    assert ids.arxiv("math.AP/0601001") == ("math/0601001", None)
    assert ids.arxiv("arXiv:math.AP/0601001v2") == ("math/0601001", "v2")
    assert ids.arxiv("arXiv:cond-mat.str-el/0601001") == ("cond-mat/0601001", None)
    assert ids.arxiv("arXiv:2101.00001v3 [math.AP]") == ("2101.00001", "v3")
    assert ids.arxiv("hep-th/9901001") == ("hep-th/9901001", None)
    with pytest.raises(ids.Invalid):
        ids.arxiv("math.AP/0613001")  # month 13
    assert ids.doi("10.5555/ABC.1,") == ids.doi("10.5555/abc.1;") == "10.5555/abc.1"
    assert ids.doi("https://doi.org/10.5555/abc.1);") == "10.5555/abc.1"
    assert ids.doi("10.5555/a(b)") == "10.5555/a(b)"  # a balanced bracket is the suffix's
    assert ids.doi("10.5555/abc.") == "10.5555/abc."  # a final dot is kept (documented)
    # In files: one work per identifier, whichever form names it.
    (lib.folder / "f5").mkdir()
    (lib.folder / "f5" / "a.bib").write_text(
        "@misc{old, title={Old style preprint}, author={Ward, K.}, year={2006},"
        " eprint={math.AP/0601001v2}, archivePrefix={arXiv}}\n"
        "@article{dot, title={Trailing punctuation}, author={Ward, K.}, year={2020},"
        " doi={10.5555/F5.1;}}\n",
        encoding="utf-8",
    )
    (lib.folder / "f5" / "b.csv").write_text(
        "key,title,arxiv,doi\n"
        "old2,Old style preprint,arXiv:math/0601001v2,\n"
        'dot2,Trailing punctuation,,"10.5555/f5.1,"\n',
        encoding="utf-8",
    )
    lib.imported("library/f5")
    old = [w for w in lib.works() if w["identifiers"].get("arxiv") == "math/0601001"]
    dot = [w for w in lib.works() if w["identifiers"].get("doi") == "10.5555/f5.1"]
    assert len(old) == 1 and len(dot) == 1
    assert lib.body(old[0]["work_id"])["identifiers"]["other"] == [
        {"scheme": "arxiv_version", "value": "v2"},
        {"scheme": "citekey", "value": "old"},
        {"scheme": "citekey", "value": "old2"},
    ]
