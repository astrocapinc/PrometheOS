"""CP11 S11.4: the evidence matrix and grounding (decisions 0064, 0065).

Behaviour tests for every clause of C11-GROUNDING, on synthetic files only
(``tools/synthetic_pdf.py``, ``tools/synthetic_bib.py``):

- every proposed or reviewed cell binds a work, a source version, a locator and a quote digest;
  a cell without evidence can only be unsupported;
- invented IDs, out-of-range locators and altered quotations are refused, naming the cell; so are
  a version of another work, a basis the evidence or the work's access state cannot support, a
  misquoted number (0.042 for 0.0042, numbers below ten included) and a carried PDF digest
  without offsets;
- metadata-only, abstract-only and full-text states stay visible in the matrix, the context and
  every export;
- an applied correction flags the dependent rows and the generated briefs in the same
  transaction; only the owner's re-review clears a row;
- reviewing is the owner's decision (approval_required for an agent); a changed reviewed cell
  returns to proposed; a column change raises the rubric and leaves older rows stale;
- rule extraction is deterministic, bounded and never replaces reviewed or hand-made cells.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import synthetic_bib as bibs
import synthetic_pdf as pdfs
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.library import rules as rule_engine

COLUMNS = [
    {
        "id": "method",
        "label": "Method",
        "value_kind": "choice",
        "instructions": "How the result was obtained.",
        "choices": ["experiment", "simulation", "review"],
    },
    {"id": "pressure", "label": "Pressure", "value_kind": "quantity", "instructions": ""},
    {"id": "drag", "label": "Drag coefficient", "value_kind": "quantity", "instructions": ""},
    {"id": "windage", "label": "Windage loss", "value_kind": "quantity", "instructions": ""},
    {"id": "finding", "label": "Key finding", "value_kind": "text", "instructions": ""},
]
SENTENCE = "The drag coefficient was 0.0042 at 10 Pa."
RULES = {
    "rules": [
        {
            "id": "experiment",
            "column": "method",
            "keywords": ["was evacuated"],
            "value": "experiment",
        },
        {"id": "review", "column": "method", "keywords": ["we review"], "value": "review"},
        {"id": "pressure", "column": "pressure", "pattern": "evacuated to {value}", "unit": "Pa"},
        {"id": "finding", "column": "finding", "pattern": "reduces windage loss by {value} %"},
    ]
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class M:
    """A library (two PDFs, three bibliography files) and a matrix "Flywheel losses" over a
    full-text, a metadata-only and an abstract-only work."""

    def __init__(self, tmp: Path, *, plain_first: bool = False) -> None:
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        folder = self.env.research / "library"
        pdfs.write_folder(
            folder, {"drag.pdf": pdfs.paper(pdfs.DRAG), "kers.pdf": pdfs.paper(pdfs.KERS)}
        )
        bibs.write_folder(folder, bibs.synthetic_set())
        if plain_first:
            return
        self.import_library()

    def import_library(self) -> None:
        plan = self.svc.library_import_plan(self.pid, OWNER, root_alias="research", path="library")
        self.svc.library_import_commit(self.pid, plan["plan_id"], OWNER)
        works = self.svc.library_works(self.pid, AGENT, limit=200)["works"]

        def find(test: Any) -> str:
            return str(next(w for w in works if test(w))["work_id"])

        self.drag = find(lambda w: w["title"].startswith("Aerodynamic"))
        self.kers = find(lambda w: w["title"].startswith("Kinetic"))
        self.bearing = find(lambda w: w["title"] == "Bearing losses in vacuum")
        self.preprint = find(lambda w: w["access_state"] == "abstract_only")
        created = self.svc.matrix_create(
            self.pid,
            AGENT,
            title="Flywheel losses",
            columns=COLUMNS,
            work_ids=[self.drag, self.bearing, self.preprint, self.kers],
        )
        self.matrix = str(created["matrix_id"])

    # ----- reads

    def body(self, record_id: str) -> dict[str, Any]:
        return dict(self.svc.get(self.pid, OWNER, record_id, None)["body"])

    def rev(self, record_id: str) -> int:
        return int(self.svc.get(self.pid, OWNER, record_id, None)["rev"])

    def show(self, caller: Any = AGENT) -> dict[str, Any]:
        return dict(self.svc.matrix_show(self.pid, caller, self.matrix))

    def row(self, work_id: str) -> dict[str, Any]:
        return next(r for r in self.show()["rows"] if r["work_id"] == work_id)

    def cell(self, work_id: str, column: str) -> dict[str, Any]:
        return next(c for c in self.row(work_id)["cells"] if c["column_id"] == column)

    def seq(self) -> int:
        return int(self.env.seq())

    # ----- evidence

    def page(self, work_id: str, page: int) -> str:
        read = self.svc.library_read(self.pid, AGENT, work_id, page=page, max_chars=8000)
        return str(read["text"])

    def span(self, work_id: str, page: int, text: str) -> dict[str, Any]:
        at = self.page(work_id, page).index(text)
        read = self.svc.library_read(
            self.pid, AGENT, work_id, page=page, start=at, end=at + len(text)
        )
        assert read["text"] == text
        return dict(read["evidence"])

    def bib_lines(self, work_id: str, field: str = "title") -> dict[str, Any]:
        """Evidence on the lines of one of the work's bibliography provenance records."""
        record = next(
            r
            for r in self.body(work_id)["field_provenance"]
            if r["basis"] == "bibliography" and r["field"] == field
        )
        return self.lines(record["version_ref"]["id"], record["locator"])

    def lines(self, version_id: str, locator: dict[str, Any]) -> dict[str, Any]:
        version = self.body(version_id)
        with closing(sqlite3.connect(self.env.store_dir / "record.sqlite")) as conn:
            raw = conn.execute(
                "SELECT data FROM payloads WHERE sha256 = ?", (version["sha256"],)
            ).fetchone()[0]
        text = bytes(raw).decode("utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        all_lines = [line.rstrip("\r") for line in text.split("\n")]
        quoted = "\n".join(all_lines[locator["start_line"] - 1 : locator["end_line"]])
        return {
            "source_id": version["source_id"],
            "version_id": version_id,
            "locator": {
                "type": "text",
                "start_line": locator["start_line"],
                "end_line": locator["end_line"],
            },
            "quote_sha256": sha(quoted),
        }

    # ----- changes

    def propose(self, work_id: str, column: str, value: str, evidence: list[Any], **kw: Any) -> Any:
        return self.svc.matrix_propose(
            self.pid,
            kw.pop("caller", AGENT),
            matrix_id=self.matrix,
            work_id=work_id,
            column_id=column,
            value_text=value,
            evidence=evidence,
            **kw,
        )

    def windage(self, **kw: Any) -> Any:
        """The windage loss "3.2 W at 10 Pa", quoted from the table on page 2."""
        return self.propose(
            self.drag,
            "windage",
            "3.2 W at 10 Pa",
            [self.span(self.drag, 2, "10 Pa: 3.2 W")],
            quantities=[{"decimal": "3.2", "unit": "W"}, {"decimal": "10", "unit": "Pa"}],
            **kw,
        )

    def coefficient(self, **kw: Any) -> Any:
        """The drag coefficient "0.0042 at 10 Pa", quoted from page 2."""
        return self.propose(
            self.drag,
            "drag",
            "0.0042 at 10 Pa",
            [self.span(self.drag, 2, SENTENCE)],
            quantities=[{"decimal": "0.0042", "unit": ""}, {"decimal": "10", "unit": "Pa"}],
            **kw,
        )

    def raw_row(
        self, work_id: str, cells: list[dict[str, Any]], caller: Any = AGENT, **kw: Any
    ) -> Any:
        """Write a row through ``apply`` directly (any caller's route): create or revise."""
        current = next((r for r in self.show(OWNER)["rows"] if r["work_id"] == work_id), None)
        body = {
            "matrix_ref": {"id": self.matrix, "rev": self.rev(self.matrix)},
            "work_ref": {"id": work_id, "rev": self.rev(work_id)},
            "cells": cells,
        }
        if current is None or current["extraction_id"] is None:
            op = {
                "op": "create",
                "kind": "extraction",
                "id": new_id("extraction"),
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        else:
            op = {
                "op": "revise",
                "id": current["extraction_id"],
                "base_rev": current["rev"],
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        return self.env.apply([op], caller=caller, **kw)


def cell(column: str, value: str, evidence: list[Any], **kw: Any) -> dict[str, Any]:
    return {
        "column_id": column,
        "value_text": value,
        "quantities": kw.pop("quantities", []),
        "evidence": evidence,
        "basis": kw.pop("basis", "full_text"),
        "state": kw.pop("state", "proposed"),
        "method": kw.pop("method", "manual"),
        "transformation": kw.pop("transformation", None),
    }


def pdf_pointer(evidence: dict[str, Any], page_text: str, written: str) -> str:
    loc = evidence["locator"]
    at = page_text.index(written, loc["start"])
    return f"pdf:{at}:{at + len(written)}"


@pytest.fixture
def m(tmp_path: Path) -> M:
    return M(tmp_path)


def refused(fn: Any, code: str = "validation_error") -> PrometheosError:
    with pytest.raises(PrometheosError) as caught:
        fn()
    assert caught.value.code == code, (caught.value.code, caught.value.message)
    return caught.value


# ----- C11-GROUNDING: a cell binds a work, a version, a locator and a quote digest ---------------


def test_a_quoted_quantity_binds_work_version_locator_and_digest(m: M) -> None:
    done = m.coefficient()
    stored = m.body(done["extraction_id"])
    assert stored["work_ref"]["id"] == m.drag and stored["matrix_ref"]["id"] == m.matrix
    (only,) = stored["cells"]
    assert only["state"] == "proposed" and only["basis"] == "full_text"
    (ev,) = only["evidence"]
    work = m.svc.library_work(m.pid, AGENT, m.drag)["work"]
    assert ev["version_id"] == work["pdf_version_id"]
    assert ev["locator"]["type"] == "pdf" and ev["locator"]["page"] == 2
    assert ev["quote_sha256"] == sha(SENTENCE)
    page = m.page(m.drag, 2)
    assert page[ev["locator"]["start"] : ev["locator"]["end"]] == SENTENCE
    pointers = {q["decimal"]: q["source_pointer"] for q in only["quantities"]}
    assert pointers == {
        "0.0042": pdf_pointer(ev, page, "0.0042"),
        "10": pdf_pointer(ev, page, "10 Pa"),
    }
    # The view shows where to open the reader.
    shown = m.cell(m.drag, "drag")["evidence"][0]["where"]
    assert shown == {
        "work_id": m.drag,
        "page": 2,
        "start": ev["locator"]["start"],
        "end": ev["locator"]["end"],
    }


def test_a_cell_without_evidence_can_only_be_unsupported(m: M) -> None:
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "Something", [])]))
    assert "invalid extraction body" in error.message
    assert "without evidence can only be unsupported" in json.dumps(error.details)
    # An unsupported cell claims no value: an empty text or a "not found" reason, no quantity.
    refused(lambda: m.raw_row(m.drag, [cell("finding", "12 W", [], state="unsupported")]))
    m.raw_row(m.drag, [cell("finding", "not found: no match", [], state="unsupported")])
    m.raw_row(m.drag, [cell("finding", "", [], state="unsupported")])
    assert m.cell(m.drag, "finding")["state"] == "unsupported"
    # A proposed cell with an empty value is refused too.
    span = m.span(m.drag, 2, SENTENCE)
    refused(lambda: m.raw_row(m.drag, [cell("finding", " ", [span])]))


def test_invented_ids_are_refused_naming_the_cell(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    invented = {**span, "version_id": "version_00000000-0000-4000-8000-0000000000ff"}
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [invented])]), "not_found")
    assert error.message.startswith("cell finding:") and error.details["cell"] == "finding"
    ghost = {**span, "source_id": "source_00000000-0000-4000-8000-0000000000fe"}
    refused(lambda: m.propose(m.drag, "finding", "x", [ghost]), "not_found")
    # An invented work or matrix is refused before any cell is looked at.
    refused(
        lambda: m.svc.matrix_propose(
            m.pid,
            AGENT,
            matrix_id=m.matrix,
            work_id="work_00000000-0000-4000-8000-0000000000fd",
            column_id="finding",
            value_text="x",
            evidence=[span],
        ),
        "not_found",
    )
    body = {
        "matrix_ref": {"id": "matrix_00000000-0000-4000-8000-0000000000fc", "rev": 1},
        "work_ref": {"id": m.drag, "rev": m.rev(m.drag)},
        "cells": [],
    }
    op = {
        "op": "create",
        "kind": "extraction",
        "id": new_id("extraction"),
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": body,
    }
    refused(lambda: m.env.apply([op]), "not_found")
    # An unknown column names the cell too.
    error = refused(lambda: m.raw_row(m.drag, [cell("color", "x", [span])]))
    assert error.message.startswith("cell color:")


def test_out_of_range_locators_are_refused(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    loc = span["locator"]
    for bad, message in (
        ({**loc, "page": 9}, "page is outside"),
        ({**loc, "start": 0, "end": 100_000}, "outside the page text"),
    ):
        error = refused(
            lambda bad=bad: m.raw_row(m.drag, [cell("finding", "x", [{**span, "locator": bad}])])
        )
        assert message in error.message and error.message.startswith("cell finding:")
    # Lines beyond a bibliography file.
    lines = m.bib_lines(m.bearing)
    far = {**lines, "locator": {"type": "text", "start_line": 9000, "end_line": 9001}}
    error = refused(lambda: m.raw_row(m.bearing, [cell("finding", "x", [far], basis="metadata")]))
    assert "outside the version" in error.message


def test_an_altered_quotation_is_refused(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    loc = span["locator"]
    altered = {**span, "quote_sha256": sha(SENTENCE.replace("0.0042", "0.042"))}
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [altered])]))
    assert "quote digest does not match" in error.message
    shifted = {**span, "locator": {**loc, "start": loc["start"] + 1}}
    refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [shifted])]))
    # A digest is required: a cell never cites a span without one.
    bare = {**span, "quote_sha256": None}
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [bare])]))
    assert "quote digest" in error.message


def test_a_version_of_another_work_is_refused(m: M) -> None:
    kers = m.span(m.kers, 1, "We review kinetic energy recovery systems.")
    error = refused(lambda: m.propose(m.drag, "finding", "We review", [kers]))
    assert "does not belong to the row's work" in error.message
    # The same span is fine in its own work's row.
    m.propose(m.kers, "method", "review", [kers])
    # Another work's bibliography lines in the same file do not ground a metadata cell either.
    other = m.bib_lines(m.preprint)
    error = refused(lambda: m.raw_row(m.bearing, [cell("finding", "x", [other], basis="metadata")]))
    assert "does not belong" in error.message or "bibliography entry" in error.message


def test_the_basis_agrees_with_the_evidence_and_the_access_state(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    # Full text on a metadata-only work is refused: the work has no text to quote.
    lines = m.bib_lines(m.bearing)
    error = refused(lambda: m.raw_row(m.bearing, [cell("finding", "Bearing losses", [lines])]))
    assert "this work is metadata_only" in error.message
    # A metadata basis cites the work's own bibliography lines, not its PDF.
    m.raw_row(m.bearing, [cell("finding", "Bearing losses in vacuum", [lines], basis="metadata")])
    refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [span], basis="metadata")]))
    # Abstract basis: inside the abstract span of a full-text work, or an abstract-only work's
    # abstract lines; not elsewhere, and not on a metadata-only work.
    abstract = m.body(m.drag)["abstract_ref"]
    page_one = m.page(m.drag, 1)
    inside = abstract["locator"]
    quoted = page_one[inside["start"] : inside["end"]]
    work = m.svc.library_work(m.pid, AGENT, m.drag)["work"]
    cited = {
        "source_id": work["pdf_source_id"],
        "version_id": work["pdf_version_id"],
        "locator": inside,
        "quote_sha256": sha(quoted),
    }
    m.raw_row(
        m.drag,
        [
            cell(
                "windage",
                "3.2 W",
                [cited],
                basis="abstract",
                quantities=[
                    {
                        "decimal": "3.2",
                        "unit": "W",
                        "source_ref": cited,
                        "source_pointer": pdf_pointer(cited, page_one, "3.2 W"),
                    }
                ],
            )
        ],
    )
    outside = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [span], basis="abstract")]))
    assert "outside the work's abstract" in outside.message or "abstract" in outside.message
    pre = m.body(m.preprint)["abstract_ref"]
    pre_lines = m.lines(pre["version_id"], pre["locator"])
    m.raw_row(
        m.preprint, [cell("finding", "composite flywheel rotors", [pre_lines], basis="abstract")]
    )
    error = refused(lambda: m.raw_row(m.bearing, [cell("finding", "x", [lines], basis="abstract")]))
    assert "this work is metadata_only" in error.message
    # An abstract-only work has no full text.
    error = refused(
        lambda: m.raw_row(m.preprint, [cell("finding", "x", [pre_lines], basis="full_text")])
    )
    assert "this work is abstract_only" in error.message


def test_misquoted_numbers_are_refused_without_tolerance(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    page = m.page(m.drag, 2)
    # Through the service: the value is located in the quote, or the proposal is refused.
    error = refused(
        lambda: m.propose(
            m.drag, "drag", "0.042", [span], quantities=[{"decimal": "0.042", "unit": ""}]
        )
    )
    assert "0.042 does not occur verbatim" in error.message
    # Through apply with an explicit pointer: the same rule, whoever the caller.
    at = page.index("0.0042")
    for decimal, unit, pointer in (
        ("0.042", "", f"pdf:{at}:{at + 6}"),  # the digits of 0.0042, claimed as 0.042
        ("0.042", "", f"pdf:{at + 1}:{at + 6}"),  # a slice of a longer number
        ("0.00420", "", f"pdf:{at}:{at + 6}"),  # a reformatted value
        ("10", "kPa", pdf_pointer(span, page, "10 Pa")),  # another unit
    ):
        q = {"decimal": decimal, "unit": unit, "source_ref": span, "source_pointer": pointer}
        value = f"{decimal} {unit}".strip()
        refused(lambda q=q, v=value: m.raw_row(m.drag, [cell("drag", v, [span], quantities=[q])]))
    # A pointer outside the quoted span is refused even when the number is right there.
    other = m.span(m.drag, 2, "1000 Pa: 41.5 W")
    loose = {
        "decimal": "0.0042",
        "unit": "",
        "source_ref": other,
        "source_pointer": f"pdf:{at}:{at + 6}",
    }
    error = refused(
        lambda: m.raw_row(m.drag, [cell("drag", "0.0042", [other], quantities=[loose])])
    )
    assert "outside the quoted evidence span" in error.message
    # Numbers below ten: 3.2 W is in the table (page 2), 3.20 W and 3 W are not.
    table = m.span(m.drag, 2, "10 Pa: 3.2 W")
    m.propose(m.drag, "windage", "3.2 W", [table], quantities=[{"decimal": "3.2", "unit": "W"}])
    for wrong in ("3.20", "3"):
        refused(
            lambda w=wrong: m.propose(
                m.drag, "windage", f"{w} W", [table], quantities=[{"decimal": w, "unit": "W"}]
            )
        )
    # A quantity cell states no number it does not bind; a text cell none it does not quote.
    error = refused(
        lambda: m.propose(
            m.drag,
            "windage",
            "3.2 W at 15 Pa",
            [table],
            quantities=[{"decimal": "3.2", "unit": "W"}],
        )
    )
    assert error.details["numbers"] == ["15"]
    # 10 is quoted in the span ("10 Pa: 3.2 W") but not bound: a quantity cell still refuses it.
    error = refused(
        lambda: m.propose(
            m.drag,
            "windage",
            "3.2 W at 10 Pa",
            [table],
            quantities=[{"decimal": "3.2", "unit": "W"}],
        )
    )
    assert error.details["numbers"] == ["10"] and "not bound quantities" in error.message
    error = refused(lambda: m.propose(m.drag, "finding", "drag of 0.042", [span]))
    assert error.details["numbers"] == ["0.042"]
    m.propose(m.drag, "finding", "drag coefficient 0.0042 at 10 Pa", [span])
    # A quantity column needs a bound quantity.
    refused(lambda: m.propose(m.drag, "windage", "see the table", [table]))


def test_choice_values_are_checked(m: M) -> None:
    span = m.span(m.drag, 1, "The housing was evacuated to 10 Pa with a scroll pump.")
    error = refused(lambda: m.propose(m.drag, "method", "measurement", [span]))
    assert "not one of the column's choices" in error.message
    m.propose(m.drag, "method", "experiment", [span])
    assert m.cell(m.drag, "method")["value_text"] == "experiment"


def test_a_carried_pdf_digest_without_offsets_never_grounds_a_cell(tmp_path: Path) -> None:
    m = M(tmp_path, plain_first=True)
    commit = m.env.import_files(["library/drag.pdf"])["commit"]
    source_id, version_id = (r["id"] for r in commit["results"])
    old = {
        "source_id": source_id,
        "version_id": version_id,
        "locator": {"type": "pdf", "page": 2, "bbox": None},
        "quote_sha256": sha(SENTENCE),
    }
    entry = new_id("entry")
    m.env.apply([create_entry(entry, title="Read before the library", evidence=[old])])
    m.import_library()
    assert m.svc.library_work(m.pid, AGENT, m.drag)["work"]["pdf_version_id"] == version_id
    # The entry carrying it stays revisable (S11.1 review F1) ...
    body = m.body(entry)
    m.env.apply(
        [
            {
                "op": "revise",
                "id": entry,
                "base_rev": 1,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {**body, "title": "Read before the library (kept)"},
            }
        ]
    )
    # ... but the same reference never grounds a matrix cell, as proposed or as revised.
    error = refused(lambda: m.propose(m.drag, "finding", "drag coefficient", [old]))
    assert "without character offsets" in error.message and error.details["cell"] == "finding"
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "drag", [old])]))
    assert "without character offsets" in error.message


# ----- access states stay visible ----------------------------------------------------------------


def test_access_states_stay_visible_in_the_matrix_context_and_exports(m: M) -> None:
    m.coefficient()
    m.svc.matrix_unsupported(
        m.pid,
        AGENT,
        matrix_id=m.matrix,
        work_id=m.bearing,
        column_id="windage",
        reason="metadata only: no text to quote",
    )
    shown = m.show()
    states = {r["work_id"]: r["access_state"] for r in shown["rows"]}
    assert states == {
        m.drag: "full_text",
        m.bearing: "metadata_only",
        m.preprint: "abstract_only",
        m.kers: "full_text",
    }
    assert shown["counts"]["access_states"] == {
        "abstract_only": 1,
        "full_text": 2,
        "metadata_only": 1,
    }
    assert shown["content_trust"] == "untrusted_data" and "not instructions" in shown["notice"]
    unsupported = m.cell(m.bearing, "windage")
    assert unsupported["state"] == "unsupported" and unsupported["evidence"] == []
    assert unsupported["value_text"] == "not found: metadata only: no text to quote"
    context = m.svc.context(m.pid, AGENT)
    text = context["text"]
    assert "MATRIX 'Flywheel losses'" in text
    assert "metadata_only 1" in text and "abstract_only 1" in text
    assert "[metadata_only] cells reviewed 0, proposed 0, unsupported 1" in text
    assert any(s["name"] == "matrices" for s in context["manifest"]["sections"])
    csv_out = m.svc.matrix_export(m.pid, AGENT, m.matrix, "csv")["content"]
    header, *lines = csv_out.splitlines()
    assert header.startswith("work_id,work_title,access_state,")
    assert any(",metadata_only," in line and "unsupported (not found" in line for line in lines)
    assert any(",abstract_only," in line for line in lines)
    data = json.loads(m.svc.matrix_export(m.pid, AGENT, m.matrix, "json")["content"])
    assert {r["access_state"] for r in data["rows"]} == {
        "full_text",
        "metadata_only",
        "abstract_only",
    }
    brief = m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")
    content = brief["content"]
    assert "access: metadata_only" in content and "access: abstract_only" in content
    assert "unsupported (not found: metadata only: no text to quote)" in content
    loc = m.cell(m.drag, "drag")["evidence"][0]["locator"]
    assert f"p.2 [{loc['start']}:{loc['end']}] sha256:{sha(SENTENCE)[:12]}" in content


# ----- the generated brief -----------------------------------------------------------------------


def test_the_brief_is_a_generated_version_used_in_by_its_rows(m: M) -> None:
    done = m.coefficient()
    row = done["extraction_id"]
    before = m.seq()
    brief = m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")
    version_id = brief["brief"]["version_id"]
    version = m.body(version_id)
    assert version["payload_schema"] == "text" and version["mime"] == "text/markdown"
    assert version["sha256"] == sha(brief["content"])
    assert version["transformation"]["type"] == "conversion"
    assert version["transformation"]["engine"] == "prometheos-matrix-brief"
    source = m.body(version["source_id"])
    assert source["title"] == "Brief: Flywheel losses"
    # It records the sequence and the revisions it reports.
    assert f"at project_seq {before}." in brief["content"]
    assert f"{row} rev {m.rev(row)}" in brief["content"]
    assert brief["brief"]["built_at_seq"] == before and m.seq() == before + 1
    with closing(sqlite3.connect(m.env.store_dir / "record.sqlite")) as conn:
        uses = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT source_id, relation FROM relations WHERE target_id = ?", (version_id,)
            )
        }
    assert uses == {(m.matrix, "used_in"), (row, "used_in")}
    # A second brief is a new version of the same brief source.
    again = m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")["brief"]
    assert again["source_id"] == brief["brief"]["source_id"]
    assert again["version_id"] != version_id
    assert [b["version_id"] for b in m.show()["briefs"]] == [again["version_id"], version_id]


# ----- corrections reach rows and briefs ---------------------------------------------------------


def assumption(m: M, title: str, text: str) -> str:
    entry = new_id("entry")
    m.env.apply([create_entry(entry, title=title, text=text, category="assumption")])
    return entry


def supersede(m: M, target: str, replacement: str) -> tuple[str, int]:
    correction = new_id("correction")
    body = {
        "target_refs": [{"id": target, "rev": m.rev(target)}],
        "replacement_refs": [{"id": replacement, "rev": 1}],
        "reason": "the loss model was refitted",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": target, "rev": m.rev(target)},
                "effect": "supersede",
                "replacement_ref": {"id": replacement, "rev": 1},
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    m.env.apply(
        [
            {
                "op": "create",
                "kind": "correction",
                "id": correction,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        ]
    )
    done = m.env.apply_protected([{"op": "correction_apply", "id": correction, "base_rev": 1}])
    return correction, int(done["project_seq"])


def test_an_applied_correction_flags_the_row_and_the_brief_in_one_transaction(m: M) -> None:
    relied = assumption(m, "Windage scales with p^0.8", "Assume windage scales with p to 0.8.")
    page = m.page(m.drag, 2)
    loss = m.span(m.drag, 2, "Losses scale with p^0.8.")
    assert loss["locator"]["end"] <= len(page)
    row = m.propose(m.drag, "finding", "Losses scale with p^0.8.", [loss], relies_on=[relied])
    row_id = row["extraction_id"]
    with closing(sqlite3.connect(m.env.store_dir / "record.sqlite")) as conn:
        depends = conn.execute(
            "SELECT target_id FROM relations WHERE source_id = ? AND relation = 'depends_on'",
            (row_id,),
        ).fetchall()
    assert depends == [(relied,)]
    brief = m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")["brief"]["version_id"]
    other = m.coefficient()  # the same row: a later revision still relies on the assumption
    assert other["extraction_id"] == row_id
    replacement = assumption(m, "Windage scales with p^0.9", "Assume p to 0.9.")
    correction, seq = supersede(m, relied, replacement)
    shown = m.show()
    flagged_row = next(r for r in shown["rows"] if r["extraction_id"] == row_id)
    assert flagged_row["needs_review"] is True
    assert flagged_row["flags"] == [
        {"reason": "applied correction", "cause_id": correction, "created_seq": seq}
    ]
    (flagged_brief,) = [b for b in shown["briefs"] if b["version_id"] == brief]
    assert flagged_brief["needs_review"] is True
    assert flagged_brief["flags"][0]["created_seq"] == seq  # the same transaction
    assert row_id in flagged_brief["flags"][0]["reason"]  # reached through the row
    assert shown["counts"]["needs_review"] == 1
    assert "NEEDS REVIEW" in m.svc.context(m.pid, AGENT)["text"]
    assert "NEEDS REVIEW" in m.svc.matrix_export(m.pid, AGENT, m.matrix, "csv")["content"] or (
        ",yes," in m.svc.matrix_export(m.pid, AGENT, m.matrix, "csv")["content"]
    )
    # The CP18 event keeps its kinds: the brief version is counted, the row is flagged only.
    events = m.svc.event_list(
        m.pid, OWNER, event_type="correction.flagged_dependents", state="all"
    )["items"]
    (event,) = [e for e in events if e["subject_ref"]["id"] == correction]
    assert event["data"]["dependents"] == {"version": 1}
    assert event["data"]["total"] == 1


def test_a_withdrawn_claim_flags_the_rows_that_rely_on_it(m: M) -> None:
    claim = new_id("claim")
    m.env.apply(
        [
            {
                "op": "create",
                "kind": "claim",
                "id": claim,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {
                    "statement": "Vacuum cuts windage loss by an order of magnitude.",
                    "scope": {
                        "system": "flywheel",
                        "operating_regime": "10 Pa",
                        "comparator": "1000 Pa",
                        "model_fidelity": "test rig",
                        "validation_level": "single study",
                        "objective": None,
                    },
                    "evidence": [],
                    "state": "proposed",
                    "assessment_refs": [],
                    "superseded_by": None,
                },
            }
        ]
    )
    row = m.coefficient(relies_on=[claim])["extraction_id"]
    correction = new_id("correction")
    body = {
        "target_refs": [{"id": claim, "rev": 1}],
        "replacement_refs": [],
        "reason": "the rig leaked",
        "evidence": [],
        "effects": [
            {
                "target_ref": {"id": claim, "rev": 1},
                "effect": "withdraw",
                "replacement_ref": None,
                "replacement_text": None,
            }
        ],
        "status": "proposed",
        "applied_seq": None,
    }
    m.env.apply(
        [
            {
                "op": "create",
                "kind": "correction",
                "id": correction,
                "base_rev": 0,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": body,
            }
        ]
    )
    m.env.apply_protected([{"op": "correction_apply", "id": correction, "base_rev": 1}])
    assert next(r for r in m.show()["rows"] if r["extraction_id"] == row)["needs_review"]


def test_only_the_owners_re_review_clears_a_flag(m: M) -> None:
    relied = assumption(m, "Windage scales with p^0.8", "Assume p to 0.8.")
    row = m.coefficient(relies_on=[relied])["extraction_id"]
    m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, authority=OWNER_ACT)
    supersede(m, relied, assumption(m, "p^0.9", "Assume p to 0.9."))
    assert m.row(m.drag)["needs_review"]
    # An agent's proposal on another cell keeps the flag.
    span = m.span(m.drag, 1, "The housing was evacuated to 10 Pa with a scroll pump.")
    m.propose(m.drag, "method", "experiment", [span])
    assert m.row(m.drag)["needs_review"]
    # An agent's unchanged revision of the flagged row is a re-review: the owner's decision.
    before = m.seq()
    refused(
        lambda: m.svc.matrix_review(
            m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, column_ids=["drag"]
        ),
        "approval_required",
    )
    assert m.seq() == before and m.row(m.drag)["needs_review"]
    # The owner re-reviews the already reviewed cell: the flag clears.
    m.svc.matrix_review(
        m.pid,
        AGENT,
        matrix_id=m.matrix,
        work_id=m.drag,
        column_ids=["drag"],
        authority=OWNER_ACT,
    )
    after = m.row(m.drag)
    assert not after["needs_review"] and after["extraction_id"] == row
    assert m.cell(m.drag, "drag")["state"] == "reviewed"
    assert m.cell(m.drag, "method")["state"] == "proposed"


# ----- the owner reviews -------------------------------------------------------------------------


def test_reviewing_is_the_owners_decision(m: M) -> None:
    row = m.coefficient()["extraction_id"]
    before = m.seq()
    error = refused(
        lambda: m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag),
        "approval_required",
    )
    approval = error.details["approval_request_id"]
    assert m.seq() == before and m.cell(m.drag, "drag")["state"] == "proposed"
    # Creating a row with a reviewed cell directly is the same protected effect.
    span = m.span(m.kers, 1, "We review kinetic energy recovery systems.")
    refused(
        lambda: m.raw_row(m.kers, [cell("method", "review", [span], state="reviewed")]),
        "approval_required",
    )
    # The owner grants the agent's request in the hub; the agent resumes it.
    shown = m.svc.approval_owner_view(m.pid, approval)
    assert any("review matrix cell drag" in s for s in shown["summary"])
    m.svc.approval_decide(
        m.pid, approval, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    resumed = m.svc.approval_resume(m.pid, AGENT, approval)
    assert resumed["state"] == "used"
    assert m.cell(m.drag, "drag")["state"] == "reviewed" and m.rev(row) == 2


def test_a_changed_reviewed_cell_returns_to_proposed(m: M) -> None:
    m.windage()
    m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, authority=OWNER_ACT)
    assert m.cell(m.drag, "windage")["state"] == "reviewed"
    # An agent proposing another value for the reviewed cell returns it to proposed.
    table = m.span(m.drag, 2, "1000 Pa: 41.5 W")
    m.propose(
        m.drag,
        "windage",
        "41.5 W at 1000 Pa",
        [table],
        quantities=[{"decimal": "41.5", "unit": "W"}, {"decimal": "1000", "unit": "Pa"}],
    )
    changed = m.cell(m.drag, "windage")
    assert changed["state"] == "proposed" and changed["value_text"] == "41.5 W at 1000 Pa"
    # Keeping it reviewed while changing it is the owner's act; so is marking it unsupported.
    m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, authority=OWNER_ACT)
    current = m.body(m.row(m.drag)["extraction_id"])["cells"]
    edited = [{**c, "value_text": "41.5 W at 1000 Pa (air)"} for c in current]
    refused(lambda: m.raw_row(m.drag, edited), "approval_required")
    refused(
        lambda: m.svc.matrix_unsupported(
            m.pid,
            AGENT,
            matrix_id=m.matrix,
            work_id=m.drag,
            column_id="windage",
            reason="unclear",
        ),
        "approval_required",
    )
    m.svc.matrix_unsupported(
        m.pid,
        AGENT,
        matrix_id=m.matrix,
        work_id=m.drag,
        column_id="windage",
        reason="unclear",
        authority=OWNER_ACT,
    )
    assert m.cell(m.drag, "windage")["state"] == "unsupported"


def test_a_row_keeps_its_matrix_and_work_and_is_unique(m: M) -> None:
    row = m.coefficient()["extraction_id"]
    span = m.span(m.drag, 2, SENTENCE)
    body = {
        "matrix_ref": {"id": m.matrix, "rev": m.rev(m.matrix)},
        "work_ref": {"id": m.drag, "rev": m.rev(m.drag)},
        "cells": [cell("finding", "drag", [span])],
    }
    op = {
        "op": "create",
        "kind": "extraction",
        "id": new_id("extraction"),
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": body,
    }
    refused(lambda: m.env.apply([op]), "revision_conflict")
    moved = {**m.body(row), "work_ref": {"id": m.kers, "rev": m.rev(m.kers)}, "cells": []}
    refused(
        lambda: m.env.apply(
            [
                {
                    "op": "revise",
                    "id": row,
                    "base_rev": m.rev(row),
                    "sensitivity": "normal",
                    "occurred_at": None,
                    "body": moved,
                }
            ]
        )
    )
    # A work outside the matrix has no row.
    outside = m.svc.library_works(m.pid, AGENT, limit=200)["works"]
    stray = next(
        w["work_id"]
        for w in outside
        if w["work_id"] not in (m.drag, m.kers, m.bearing, m.preprint)
        and w["access_state"] == "metadata_only"
    )
    error = refused(
        lambda: m.raw_row(stray, [cell("finding", "", [], state="unsupported", basis="metadata")])
    )
    assert "not in the matrix" in error.message


def test_a_column_change_raises_the_rubric_and_older_rows_are_stale(m: M) -> None:
    m.coefficient()
    current = m.body(m.matrix)
    columns = [
        *current["columns"],
        {"id": "speed", "label": "Speed", "value_kind": "quantity", "instructions": ""},
    ]
    revise = {
        "op": "revise",
        "id": m.matrix,
        "base_rev": m.rev(m.matrix),
        "sensitivity": "normal",
        "occurred_at": None,
        "body": {**current, "columns": columns},
    }
    error = refused(lambda: m.env.apply([revise]))
    assert "raise rubric_version" in error.message
    m.env.apply([{**revise, "body": {**current, "columns": columns, "rubric_version": 2}}])
    row = m.row(m.drag)
    assert row["stale"] is True and row["rubric_version"] == 1
    assert "STALE" in m.svc.context(m.pid, AGENT)["text"]
    assert "STALE (rubric 1)" in m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")["content"]
    # A new proposal re-extracts the row under the current rubric.
    span = m.span(m.drag, 2, SENTENCE)
    m.propose(m.drag, "finding", "drag coefficient", [span])
    assert m.row(m.drag)["stale"] is False
    # The rubric never decreases.
    latest = m.body(m.matrix)
    refused(
        lambda: m.env.apply(
            [{**revise, "base_rev": m.rev(m.matrix), "body": {**latest, "rubric_version": 1}}]
        )
    )


# ----- rules -------------------------------------------------------------------------------------


def run_rules(m: M, rules: dict[str, Any] = RULES, caller: Any = AGENT) -> dict[str, Any]:
    return dict(m.svc.matrix_extract(m.pid, caller, matrix_id=m.matrix, rules=rules))


def test_rules_propose_grounded_cells_and_say_no_match(m: M) -> None:
    out = run_rules(m)
    assert out["counts"] == {"created": 4} and out["run_id"].startswith("oprun_")
    drag = {c["column_id"]: c for c in m.row(m.drag)["cells"]}
    assert drag["method"]["value_text"] == "experiment" and drag["method"]["method"] == "rule"
    assert drag["method"]["rule"] == {"engine": "prometheos-matrix-rule:experiment", "version": "1"}
    assert drag["pressure"]["value_text"] == "10 Pa"
    assert drag["pressure"]["quantities"][0]["decimal"] == "10"
    assert drag["finding"]["value_text"] == "reduces windage loss by 92 %"
    assert all(c["state"] == "proposed" and c["basis"] == "full_text" for c in drag.values())
    # Each hit quotes its exact span (the stored digest is the digest of that span).
    page_one = m.page(m.drag, 1)
    ev = drag["pressure"]["evidence"][0]
    assert sha(page_one[ev["where"]["start"] : ev["where"]["end"]]) == ev["quote_sha256"]
    assert page_one[ev["where"]["start"] : ev["where"]["end"]] == "evacuated to 10 Pa"
    kers = {c["column_id"]: c for c in m.row(m.kers)["cells"]}
    assert kers["method"]["value_text"] == "review"
    assert kers["pressure"]["state"] == "unsupported"
    assert kers["pressure"]["value_text"] == "not found: no match"
    bearing = {c["column_id"]: c for c in m.row(m.bearing)["cells"]}
    assert {c["state"] for c in bearing.values()} == {"unsupported"}
    assert {c["basis"] for c in bearing.values()} == {"metadata"}
    preprint = {c["column_id"]: c for c in m.row(m.preprint)["cells"]}
    assert {c["basis"] for c in preprint.values()} == {"abstract"}


def test_rule_runs_are_deterministic_and_idempotent(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = M(tmp_path / "a"), M(tmp_path / "b")
    run_rules(first)
    run_rules(second)

    def cells(m: M) -> list[Any]:
        out = []
        for row in m.show()["rows"]:
            for c in row["cells"]:
                out.append(
                    (
                        row["work_title"],
                        c["column_id"],
                        c["value_text"],
                        c["state"],
                        c["basis"],
                        [e["locator"] for e in c["evidence"]],
                        [e["quote_sha256"] for e in c["evidence"]],
                        [(q["decimal"], q["unit"], q["source_pointer"]) for q in c["quantities"]],
                    )
                )
        return sorted(out, key=repr)

    assert cells(first) == cells(second)
    seq = first.seq()
    again = run_rules(first)
    assert again["counts"] == {"unchanged": 4} and first.seq() == seq


def test_rules_never_replace_reviewed_or_hand_made_cells(m: M) -> None:
    m.coefficient()
    span = m.span(m.drag, 1, "The housing was evacuated to 10 Pa with a scroll pump.")
    m.propose(m.drag, "pressure", "10 Pa", [span], quantities=[{"decimal": "10", "unit": "Pa"}])
    m.svc.matrix_review(
        m.pid,
        AGENT,
        matrix_id=m.matrix,
        work_id=m.drag,
        column_ids=["drag"],
        authority=OWNER_ACT,
    )
    before = m.body(m.row(m.drag)["extraction_id"])["cells"]
    run_rules(m)
    after = {c["column_id"]: c for c in m.body(m.row(m.drag)["extraction_id"])["cells"]}
    kept = {c["column_id"]: c for c in before}
    assert after["drag"] == kept["drag"] and after["drag"]["state"] == "reviewed"
    assert after["pressure"] == kept["pressure"] and after["pressure"]["method"] == "manual"
    assert after["method"]["method"] == "rule"


def test_rule_sets_are_bounded_and_checked(m: M) -> None:
    def bad(rules: Any, code: str = "validation_error") -> PrometheosError:
        return refused(lambda: run_rules(m, rules), code)

    bad({"rules": []})
    bad({"rules": [{"id": f"r{i}", "column": "finding", "keywords": ["x"]} for i in range(101)]})
    bad({"rules": [{"id": "p", "column": "finding", "pattern": "x " * 120}]})
    bad({"rules": [{"id": "p", "column": "finding", "pattern": "(a+)+$ {x}"}]})
    bad({"rules": [{"id": "p", "column": "pressure", "pattern": "{value} {value}", "unit": "Pa"}]})
    bad({"rules": [{"id": "p", "column": "pressure", "keywords": ["vacuum"]}]})
    bad({"rules": [{"id": "p", "column": "method", "keywords": ["vacuum"], "value": "guess"}]})
    bad({"rules": [{"id": "p", "column": "nope", "keywords": ["vacuum"]}]})
    bad({"rules": [{"id": "p", "column": "finding", "keywords": ["a"], "pattern": "b"}]})
    bad({"rules": [{"id": "p", "column": "finding", "keywords": ["a"], "code": "import os"}]})
    # Regular-expression syntax is literal text: it matches itself, never as an expression.
    pattern = rule_engine.compile_rule(
        rule_engine.Rule(id="lit", column="finding", pattern="(a+)+ [x]*"), "text"
    )
    assert pattern.search("aaaa xxxx") is None and pattern.search("(a+)+ [x]*") is not None


def test_rule_matching_has_no_backtracking_bomb_and_a_time_limit(
    m: M, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "a " * 50_000 + "b"
    rule = rule_engine.Rule(id="k", column="finding", keywords=[" ".join(["a"] * 20) + " c"])
    compiled = rule_engine.compile_rule(rule, "text")
    started = time.monotonic()
    assert compiled.search(hostile) is None
    assert time.monotonic() - started < 2.0
    quantity = rule_engine.compile_rule(
        rule_engine.Rule(id="q", column="pressure", pattern="to {value}", unit="Pa"), "quantity"
    )
    started = time.monotonic()
    assert quantity.search("to " + "1" * 100_000 + "x") is None
    assert time.monotonic() - started < 2.0
    # The run stops at its time limit with a reason instead of running on.
    monkeypatch.setattr(rule_engine, "TIME_LIMIT_SECONDS", -1.0)
    error = refused(lambda: run_rules(m), "budget_exceeded")
    assert "time limit" in error.message
    # Also when nothing matches: the limit is checked between texts, not only at hits.
    nothing = {"rules": [{"id": "never", "column": "finding", "keywords": ["zebra quartz"]}]}
    error = refused(lambda: run_rules(m, nothing), "budget_exceeded")
    assert "time limit" in error.message


# ----- surfaces ----------------------------------------------------------------------------------


def test_restricted_works_and_rows_stay_hidden_from_agents(m: M) -> None:
    folder = m.env.research / "private"
    pdfs.write_folder(folder, {"buses.pdf": pdfs.paper(pdfs.BATTERY)})
    plan = m.svc.library_import_plan(
        m.pid, OWNER, root_alias="research", path="private", sensitivity="restricted"
    )
    m.svc.library_import_commit(m.pid, plan["plan_id"], OWNER)
    buses = next(
        w["work_id"]
        for w in m.svc.library_works(m.pid, OWNER, limit=200)["works"]
        if w["title"].startswith("Hybrid storage")
    )
    # An agent cannot put a work it cannot see into a matrix.
    refused(
        lambda: m.svc.matrix_create(m.pid, AGENT, title="x", columns=COLUMNS[3:], work_ids=[buses]),
        "not_found",
    )
    # The owner's matrix over the restricted work is restricted, and so is its row.
    created = m.svc.matrix_create(
        m.pid, OWNER, title="Restricted", columns=COLUMNS[3:], work_ids=[buses]
    )
    assert m.svc.get(m.pid, OWNER, created["matrix_id"], None)["sensitivity"] == "restricted"
    page = m.svc.library_read(m.pid, OWNER, buses, page=2, max_chars=8000)["text"]
    at = page.index("Fuel use fell by 14 %.")
    ev = m.svc.library_read(m.pid, OWNER, buses, page=2, start=at, end=at + 22)["evidence"]
    row = m.svc.matrix_propose(
        m.pid,
        OWNER,
        matrix_id=created["matrix_id"],
        work_id=buses,
        column_id="finding",
        value_text="Fuel use fell by 14 %.",
        evidence=[ev],
    )["extraction_id"]
    assert m.svc.get(m.pid, OWNER, row, None)["sensitivity"] == "restricted"
    listed = {x["matrix_id"] for x in m.svc.matrix_list(m.pid, AGENT)["matrices"]}
    assert created["matrix_id"] not in listed and m.matrix in listed
    refused(lambda: m.svc.matrix_show(m.pid, AGENT, created["matrix_id"]), "not_found")
    assert "Fuel use" not in m.svc.context(m.pid, AGENT)["text"]


def test_cli_creates_lists_shows_extracts_exports_and_asks_for_review(m: M, tmp_path: Path) -> None:
    from test_cp1_cli import Cli

    cli = Cli(tmp_path)
    columns = tmp_path / "columns.json"
    columns.write_text(json.dumps(COLUMNS[3:]), encoding="utf-8")
    code, made, _ = cli(
        "matrix",
        "create",
        "--title",
        "From the CLI",
        "--columns",
        str(columns),
        "--work",
        m.drag,
        "--project",
        m.pid,
    )
    assert code == 0, made
    matrix = made["matrix_id"]
    code, listed, _ = cli("matrix", "list", "--project", m.pid)
    assert code == 0 and matrix in {x["matrix_id"] for x in listed["matrices"]}
    code, shown, _ = cli("matrix", "show", matrix, "--project", m.pid)
    assert shown["rows"][0]["access_state"] == "full_text"
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [RULES["rules"][3]]}), encoding="utf-8")
    code, run, _ = cli("matrix", "extract", matrix, "--rule", str(rules), "--project", m.pid)
    assert code == 0 and run["counts"] == {"created": 1}, run
    code, exported, _ = cli("matrix", "export", matrix, "--format", "csv", "--project", m.pid)
    assert code == 0 and "reduces windage loss by 92 %" in exported["content"]
    # Without an interactive terminal a review is an approval request (exit 4).
    code, asked, _ = cli("matrix", "review", matrix, "--work", m.drag, "--project", m.pid)
    assert code == 4 and asked["error"]["code"] == "approval_required"


# ----- review round 1 repairs (review-precheck-s11-4-a) ------------------------------------------

NUMBERS = pdfs.PaperSpec(
    title="Synthetic numbers in a flywheel rig",
    authors="H. Tester",
    left=(
        "Offset was -3.2 K at the wall.",
        "Current was 1,240 W in the run.",
        "Rate was 4.2e-3 W at idle.",
        "Measured on 2021-05-04 at 10:30.",
        "Firmware v0.042 was used.",
    ),
    right=("Synthetic data only.", "Peak was 1 240 W in tests.", "Burst was 4.2E-3 W at start."),
)


def numbers_work(m: M) -> str:
    folder = m.env.research / "numbers"
    pdfs.write_folder(folder, {"numbers.pdf": pdfs.paper(NUMBERS)})
    plan = m.svc.library_import_plan(m.pid, OWNER, root_alias="research", path="numbers")
    m.svc.library_import_commit(m.pid, plan["plan_id"], OWNER)
    work = next(
        w["work_id"]
        for w in m.svc.library_works(m.pid, OWNER, limit=200)["works"]
        if w["title"].startswith("Synthetic numbers")
    )
    body = m.body(m.matrix)
    refs = [*body["work_refs"], {"id": work, "rev": m.rev(work)}]
    m.env.apply(
        [
            {
                "op": "revise",
                "id": m.matrix,
                "base_rev": m.rev(m.matrix),
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {**body, "work_refs": refs},
            }
        ]
    )
    return work


def test_f1_tokenizer_reads_signs_groups_exponents_dates_and_words() -> None:
    from prometheos.app import quantities as q

    def kinds(text: str) -> list[tuple[str, str]]:
        return [(t.kind, t.text) for t in q.tokens(text)]

    minus = chr(0x2212)
    assert kinds("Offset was -3.2 K") == [("number", "-3.2")]
    assert kinds("Offset was " + minus + "3.2 K") == [("number", minus + "3.2")]
    assert kinds("Current 1,240 W and 1" + chr(0x2009) + "240 W") == [
        ("number", "1,240"),
        ("number", "1" + chr(0x2009) + "240"),
    ]
    assert kinds("Rate 4.2e-3 W") == [("number", "4.2e-3")]
    assert kinds("On 2021-05-04 at 10:30") == [("datetime", "2021-05-04"), ("number", "10:30")]
    assert kinds("drag v0.042, r5000 and CP2") == [
        ("word", "v0.042"),
        ("word", "r5000"),
        ("word", "CP2"),
    ]
    assert kinds("range 1.5-2.0 kJ") == [("number", "1.5"), ("number", "2.0")]
    # A quantity binds the whole token: a typographic minus and an exponent are bindable.
    line = "Offset was " + minus + "3.2 K."
    at = line.index(minus)
    q.check_token(line, at, at + 6, {"decimal": "-3.2", "unit": "K"}, "p")
    line = "Rate was 4.2e-3 W at idle."
    at = line.index("4.2")
    q.check_token(line, at, at + 8, {"decimal": "4.2e-3", "unit": "W"}, "p")
    for decimal, start, end in (("4.2", at, at + 3), ("3", at + 5, at + 6)):
        with pytest.raises(PrometheosError):
            q.check_token(line, start, end, {"decimal": decimal, "unit": ""}, "p")
    assert q.unbound_numbers("Rate 4.2e-3 W.", [{"decimal": "4.2e-3", "unit": "W"}]) == []


def test_f1_numbers_in_text_cells_match_whole_tokens_of_the_quote(m: M) -> None:
    work = numbers_work(m)

    def quote(text: str) -> dict[str, Any]:
        return m.span(work, 1, text)

    offset = quote("Offset was -3.2 K at the wall.")
    current = quote("Current was 1,240 W in the run.")
    rate = quote("Rate was 4.2e-3 W at idle.")
    measured = quote("Measured on 2021-05-04 at 10:30.")
    firmware = quote("Firmware v0.042 was used.")
    for value, evidence, wrong in (
        ("offset of 3.2 K", offset, "3.2"),  # the sign dropped
        ("current 240 W", current, "240"),  # a thousands group
        ("rate 3 W", rate, "3"),  # an exponent digit
        ("measured 2022-01-01", measured, "2022-01-01"),  # another date
        ("measured at 11:45", measured, "11:45"),  # another time
        ("drag v0.042", offset, "v0.042"),  # digits glued to letters, not in this quote
    ):
        error = refused(lambda v=value, e=evidence: m.propose(work, "finding", v, [e]))
        assert error.details["numbers"] == [wrong], (value, error.details)
        assert "not in the quoted evidence" in error.message
    for value, evidence in (
        ("offset of -3.2 K", offset),
        ("offset of " + chr(0x2212) + "3.2 K", offset),  # a typographic minus is a minus
        ("current 1,240 W", current),
        ("rate 4.2e-3 W", rate),  # a faithful exponent
        ("measured 2021-05-04 at 10:30", measured),
        ("firmware v0.042", firmware),
    ):
        m.propose(work, "finding", value, [evidence])
        assert m.cell(work, "finding")["value_text"] == value
    # The quantity path reads the same tokens: the exponent binds whole, a fragment does not.
    m.propose(work, "windage", "4.2e-3 W", [rate], quantities=[{"decimal": "4.2e-3", "unit": "W"}])
    for decimal in ("4.2", "3"):
        refused(
            lambda d=decimal: m.propose(
                work, "windage", f"{d} W", [rate], quantities=[{"decimal": d, "unit": "W"}]
            )
        )
    error = refused(
        lambda: m.propose(
            work, "pressure", "240 W", [current], quantities=[{"decimal": "240", "unit": "W"}]
        )
    )
    assert "does not occur verbatim" in error.message


def test_f2_white_space_at_the_edge_of_a_rule_stays_linear() -> None:
    hostile = " " * 100_000 + "x"
    for rule in (
        rule_engine.Rule(id="k", column="finding", keywords=[" abc", "abc  ", "  a b  "]),
        rule_engine.Rule(id="p", column="finding", pattern="  reduces loss  "),
        rule_engine.Rule(id="q", column="pressure", pattern="  {value}", unit="Pa"),
    ):
        compiled = rule_engine.compile_rule(rule, "quantity" if rule.unit else "text")
        started = time.monotonic()
        assert compiled.search(hostile) is None
        assert time.monotonic() - started < 2.0, rule.id
    keyword = rule_engine.compile_rule(
        rule_engine.Rule(id="k", column="finding", keywords=["  vacuum  "]), "text"
    )
    assert keyword.search("in vacuum, the loss") is not None


def test_f3_a_refused_rule_cell_does_not_lose_the_row(
    m: M, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rule_engine.cell_for

    def one_bad(column: dict[str, Any], *args: Any, **kw: Any) -> dict[str, Any]:
        made = real(column, *args, **kw)
        if column["id"] == "finding" and made["state"] == "proposed":
            made = {**made, "value_text": made["value_text"] + " in 1999"}  # not in the quote
        return made

    monkeypatch.setattr(rule_engine, "cell_for", one_bad)
    out = run_rules(m)
    drag_row = next(r for r in out["rows"] if r["work_id"] == m.drag)
    assert drag_row["action"] == "created"
    assert set(drag_row["refused_cells"]) == {"finding"}
    assert "not in the quoted evidence" in drag_row["refused_cells"]["finding"]
    stored = {c["column_id"]: c for c in m.row(m.drag)["cells"]}
    assert drag_row["cells"] == {k: v["state"] for k, v in stored.items()}
    assert stored["method"]["state"] == "proposed" and stored["pressure"]["state"] == "proposed"
    finding = stored["finding"]
    assert finding["state"] == "unsupported" and finding["evidence"] == []
    assert finding["value_text"].startswith("not found: the rule's match was refused by grounding")
    assert not any(ch.isdigit() for ch in finding["value_text"])  # F9: no number is kept


def test_f4_a_metadata_cell_stays_inside_its_own_entry_lines(m: M) -> None:
    record = next(
        r
        for r in m.body(m.bearing)["field_provenance"]
        if r["basis"] == "bibliography" and r["field"] == "title"
    )
    loc = record["locator"]
    m.raw_row(
        m.bearing,
        [cell("finding", "x", [m.lines(record["version_ref"]["id"], loc)], basis="metadata")],
    )
    for extra in (1, 2):
        wider = {**loc, "end_line": loc["end_line"] + extra}
        lines = m.lines(record["version_ref"]["id"], wider)
        refused(lambda e=lines: m.raw_row(m.bearing, [cell("finding", "x", [e], basis="metadata")]))


def test_f4_page_one_text_before_the_abstract_is_not_the_abstract(m: M) -> None:
    abstract = m.body(m.drag)["abstract_ref"]["locator"]
    page_one = m.page(m.drag, 1)
    title = "Aerodynamic drag of a flywheel housing"
    assert page_one.index(title) < abstract["start"]
    before = m.span(m.drag, 1, title)
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [before], basis="abstract")]))
    assert "outside the work's abstract" in error.message
    # One character before the abstract is outside too.
    start = abstract["start"] - 1
    work = m.svc.library_work(m.pid, AGENT, m.drag)["work"]
    edge = {
        "source_id": work["pdf_source_id"],
        "version_id": work["pdf_version_id"],
        "locator": {**abstract, "start": start},
        "quote_sha256": sha(page_one[start : abstract["end"]]),
    }
    error = refused(lambda: m.raw_row(m.drag, [cell("finding", "x", [edge], basis="abstract")]))
    assert "outside the work's abstract" in error.message


def test_f4_changing_only_the_evidence_or_method_of_a_reviewed_cell_is_the_owners_act(
    m: M,
) -> None:
    m.coefficient()
    m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, authority=OWNER_ACT)
    (current,) = m.body(m.row(m.drag)["extraction_id"])["cells"]
    page = m.page(m.drag, 2)
    at = page.index(SENTENCE)
    wider = m.span(m.drag, 2, page[at : at + len(SENTENCE) + 7])
    moved = {
        **current,
        "evidence": [wider],
        "quantities": [{**q, "source_ref": wider} for q in current["quantities"]],
    }
    refused(lambda: m.raw_row(m.drag, [moved]), "approval_required")
    model = {
        "type": "extraction",
        "engine": "agent-model",
        "engine_version": "1",
        "model": "x",
        "language": None,
        "parameters_digest": "a" * 64,
        "input_version_refs": [],
        "run_id": None,
    }
    remethod = {**current, "method": "model", "transformation": model}
    refused(lambda: m.raw_row(m.drag, [remethod]), "approval_required")
    assert m.cell(m.drag, "drag")["state"] == "reviewed"


def test_f4_a_merged_work_gets_no_row(m: M) -> None:
    pair = next(
        c["works"]
        for c in m.svc.library_dedup(m.pid, OWNER)["candidates"]
        if c["works"][0]["title"].lower().startswith("windage in sealed")
    )
    a, b = pair[0]["work_id"], pair[1]["work_id"]
    made = m.svc.matrix_create(m.pid, AGENT, title="Windage", columns=COLUMNS[4:], work_ids=[a, b])
    m.svc.library_dedup_resolve(m.pid, a, b, "merge", authority=OWNER_ACT)
    merged = a if m.body(a).get("merged_into") else b
    error = refused(
        lambda: m.svc.matrix_unsupported(
            m.pid,
            AGENT,
            matrix_id=made["matrix_id"],
            work_id=merged,
            column_id="finding",
            reason="no text",
        )
    )
    assert "merged into another work" in error.message


def test_f4_an_interrupted_rule_run_resumes_and_replays(
    m: M, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.library.matrix_ops import MatrixExtractHandler

    real = MatrixExtractHandler._write_row
    calls = {"n": 0}

    def stop_at_second(self: Any, *args: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PrometheosError("io_error", "interrupted", {})
        return real(self, *args, **kw)

    monkeypatch.setattr(MatrixExtractHandler, "_write_row", stop_at_second)
    refused(lambda: run_rules(m), "io_error")
    monkeypatch.setattr(MatrixExtractHandler, "_write_row", real)
    (run,) = m.svc.run_list(m.pid, AGENT, operation="matrix_extract")["runs"]
    assert run["state"] == "failed"
    first = next(r for r in m.show()["rows"] if r["extraction_id"])
    seq = m.seq()
    m.svc.run_resume(m.pid, AGENT, run["run_id"], mode="foreground")
    done = m.svc.run_get(m.pid, AGENT, run["run_id"])
    assert done["state"] == "completed"
    assert done["result"]["counts"] == {"created": 3, "unchanged": 1}
    assert m.seq() == seq + 3  # the first row was not written again
    assert m.rev(first["extraction_id"]) == 1


def test_f5_the_brief_is_inert_markdown(m: M) -> None:
    hostile = "<img src=x onerror=alert()> [x](javascript:alert()) ![i](http://evil.example/a.png)"
    body = m.body(m.matrix)
    title = "Losses " + hostile + " # x a" + chr(92) + "|b"
    m.env.apply(
        [
            {
                "op": "revise",
                "id": m.matrix,
                "base_rev": m.rev(m.matrix),
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {**body, "title": title},
            }
        ]
    )
    span = m.span(m.drag, 2, SENTENCE)
    m.propose(m.drag, "finding", hostile + " drag coefficient", [span])
    content = m.svc.matrix_export(m.pid, AGENT, m.matrix, "markdown")["content"]
    bs = chr(92)
    assert "<img" not in content.replace(bs + "<img", "")
    assert "![" not in content.replace(bs + "!" + bs + "[", "")
    assert "](" not in content.replace(bs + "](", "")
    assert bs + "<img src=x onerror=alert()" + bs + ">" in content
    assert bs + "[x" + bs + "](javascript:alert())" in content
    assert "a" + bs + bs + bs + "|b" in content  # the backslash escaped, then the pipe
    for line in content.splitlines():  # every table row keeps its five columns
        if line.startswith("| ") and "---" not in line:
            assert line.replace(bs + "|", "").count("|") == 6, line


def test_f6_a_row_is_extracted_under_the_current_matrix_revision(m: M) -> None:
    m.coefficient()
    old_rev = m.rev(m.matrix)
    body = m.body(m.matrix)
    columns = [
        {**c, "choices": ["experiment", "simulation"]} if c["id"] == "method" else c
        for c in body["columns"]
    ]
    m.env.apply(
        [
            {
                "op": "revise",
                "id": m.matrix,
                "base_rev": old_rev,
                "sensitivity": "normal",
                "occurred_at": None,
                "body": {**body, "columns": columns, "rubric_version": 2},
            }
        ]
    )
    span = m.span(m.kers, 1, "We review kinetic energy recovery systems.")
    op = {
        "op": "create",
        "kind": "extraction",
        "id": new_id("extraction"),
        "base_rev": 0,
        "sensitivity": "normal",
        "occurred_at": None,
        "body": {
            "matrix_ref": {"id": m.matrix, "rev": old_rev},
            "work_ref": {"id": m.kers, "rev": m.rev(m.kers)},
            "cells": [cell("method", "review", [span])],
        },
    }
    error = refused(lambda: m.env.apply([op]))
    assert "older matrix revision" in error.message
    # A stale row may be reviewed and have cells marked unsupported, nothing else.
    row = m.row(m.drag)
    assert row["stale"]
    current = m.body(row["extraction_id"])
    sentence = m.span(m.drag, 2, SENTENCE)
    added = {**current, "cells": [*current["cells"], cell("finding", "drag", [sentence])]}
    revise = {
        "op": "revise",
        "id": row["extraction_id"],
        "base_rev": row["rev"],
        "sensitivity": "normal",
        "occurred_at": None,
        "body": added,
    }
    error = refused(lambda: m.env.apply([revise]))
    assert "cell finding" in error.message and "older matrix revision" in error.message
    m.svc.matrix_review(m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, authority=OWNER_ACT)
    m.svc.matrix_unsupported(
        m.pid, AGENT, matrix_id=m.matrix, work_id=m.drag, column_id="windage", reason="none"
    )
    assert m.row(m.drag)["stale"]
    # Re-extracting (a proposal) moves the row to the current revision.
    m.propose(m.drag, "finding", "drag coefficient", [sentence])
    assert not m.row(m.drag)["stale"]


def test_f9_an_unsupported_cell_states_no_number(m: M) -> None:
    error = refused(
        lambda: m.raw_row(
            m.drag, [cell("finding", "not found: expected 99.9 W", [], state="unsupported")]
        )
    )
    assert error.details["numbers"] == ["99.9"]
    m.svc.matrix_unsupported(
        m.pid,
        AGENT,
        matrix_id=m.matrix,
        work_id=m.drag,
        column_id="finding",
        reason="not in table 2 (expected 99.9 W on 2021-05-04)",
    )
    assert (
        m.cell(m.drag, "finding")["value_text"] == "not found: not in table # (expected # W on #)"
    )


# ----- review round 2 repairs (review-precheck-s11-4-a-r2) -----------------------------------


def test_r2_f1_many_failing_candidates_stay_linear() -> None:
    from prometheos.app import quantities as q

    # 20,000 candidates that fail the whole-token check (each "10" sits inside "1,10") on a
    # 160,000-character text: the text is tokenised once, not once per candidate.
    text = "1,10 Pa " * 20_000
    rule = rule_engine.Rule(id="p", column="pressure", pattern="{value}", unit="Pa")
    located = rule_engine.Located(
        "full_text",
        text,
        lambda s, e: {"start": s, "end": e},
        lambda s, e: f"pdf:{s}:{e}",
        "version_x",
    )
    column = {"id": "pressure", "value_kind": "quantity"}
    started = time.monotonic()
    made = rule_engine.cell_for(column, [rule], [located], "full_text", rule_engine.deadline())
    assert time.monotonic() - started < 2.0
    assert made["state"] == "unsupported"
    # The reviewer's first size, through the agent's binding path too.
    smaller = "1,10 Pa " * 5_000
    started = time.monotonic()
    assert q.find_token(smaller, "10", "Pa") is None
    assert q.find_token(text, "10", "Pa") is None
    assert time.monotonic() - started < 2.0


def test_r2_f2_record_ids_hide_no_numbers() -> None:
    from prometheos.app import quantities as q

    def kinds(text: str) -> list[tuple[str, str]]:
        return [(t.kind, t.text) for t in q.tokens(text)]

    real = "entry_00000000-0000-4000-8000-000000003200"
    assert kinds(f"see {real}.") == [("id", real)]
    # Digits glued after an ID are not part of an ID.
    glued = kinds(f"see {real}9999")
    assert ("id", real) not in glued and glued
    assert ("id", real) not in kinds(f"see {real}.5")
    # A decimal glued to a word with a full stop is a token.
    for text, token in (
        ("drag coefficient loss.5", "loss.5"),
        ("x.99", "x.99"),
        ("approx.5 W", "approx.5"),
        ("value.99 here", "value.99"),
    ):
        assert ("word", token) in kinds(text), text


def test_r2_f2_only_a_visible_record_id_is_exempt(m: M) -> None:
    span = m.span(m.drag, 2, SENTENCE)
    # A record the caller can see may be named in a value (the matrix itself here).
    m.propose(m.drag, "finding", f"drag coefficient, see {m.matrix}", [span])
    assert m.cell(m.drag, "finding")["value_text"].endswith(m.matrix)
    forged = "entry_00000000-0000-4000-8000-000000003200"
    for value, wrong in (
        (f"drag coefficient, see {forged}", forged),
        (f"drag coefficient, see {m.matrix}9999", None),
        ("drag coefficient loss.5", "loss.5"),
        ("drag coefficient approx.5", "approx.5"),
    ):
        error = refused(lambda v=value: m.propose(m.drag, "finding", v, [span]))
        if wrong is not None:
            assert wrong in error.details["numbers"], (value, error.details)
    # In an unsupported cell's reason a forged ID is no hiding place either.
    error = refused(
        lambda: m.raw_row(
            m.drag, [cell("finding", f"not found: see {forged}", [], state="unsupported")]
        )
    )
    assert error.details["numbers"] == [forged]


def test_r2_f3_space_grouped_thousands_agree_in_text_and_quantity_cells(m: M) -> None:
    from prometheos.app import quantities as q

    assert [t.text for t in q.tokens("Peak was 1 240 W")] == ["1 240"]
    assert [t.text for t in q.tokens("runs 1 2 3")] == ["1", "2", "3"]
    work = numbers_work(m)
    peak = m.span(work, 1, "Peak was 1 240 W in tests.")
    error = refused(lambda: m.propose(work, "finding", "peak 240 W", [peak]))
    assert error.details["numbers"] == ["240"]
    m.propose(work, "finding", "peak 1 240 W", [peak])
    error = refused(
        lambda: m.propose(
            work, "windage", "240 W", [peak], quantities=[{"decimal": "240", "unit": "W"}]
        )
    )
    assert "does not occur verbatim" in error.message


def test_r2_f4_binding_without_a_pointer_finds_a_minus_and_an_upper_case_exponent(m: M) -> None:
    from prometheos.app import quantities as q

    minus = chr(0x2212)
    line = "Offset was " + minus + "3.2 K at the wall."
    at = line.index(minus)
    assert q.find_token(line, "-3.2", "K") == (at, at + 6)
    line = "Rate 4.2E-3 W at peak."
    at = line.index("4.2")
    assert q.find_token(line, "4.2e-3", "W") == (at, at + 8)
    assert q.find_token(line, "4.2", "W") is None
    # Through the agent's proposal (no pointer given): the exponent is found and bound.
    work = numbers_work(m)
    burst = m.span(work, 1, "Burst was 4.2E-3 W at start.")
    m.propose(work, "windage", "4.2E-3 W", [burst], quantities=[{"decimal": "4.2E-3", "unit": "W"}])
    stored = m.cell(work, "windage")
    assert stored["state"] == "proposed" and stored["quantities"][0]["decimal"] == "4.2E-3"
