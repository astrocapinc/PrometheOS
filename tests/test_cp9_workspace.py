"""CP9.2 workspace (C09-EDITOR backend half, S20): binding, bounded files, digest-guarded
atomic writes, snapshots with anchor remap, structure query and evidence insertion.
No engine needed."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import anchor_body, claim_body, create, text_evidence
from cp9_support import LatexEnv

from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def ws(tmp_path: Path) -> LatexEnv:
    return LatexEnv(Env(tmp_path))


def _versions(ws: LatexEnv, source_id: str) -> list[dict]:
    items = ws.svc.list_records(ws.pid, OWNER, kind="version", limit=200)["items"]
    return [v for v in items if v["body"]["source_id"] == source_id]


def test_bind_points_the_paper_at_the_main_file(ws: LatexEnv) -> None:
    paper = ws.paper()
    source = ws.get(paper["body"]["manuscript_source_id"])
    assert source["body"]["root_binding"] == {"alias": "research", "path": "paper/main.tex"}
    assert source["body"]["retention"] == "copy"
    # Binding again is a no-op.
    again = ws.svc.workspace_bind(ws.pid, OWNER, ws.paper_id, "research", "paper/main.tex")
    assert again["commit"] is None
    for bad in ("paper/refs.bib", "../outside.tex", "C:/x/main.tex", "paper/missing.tex"):
        with pytest.raises(PrometheosError):
            ws.svc.workspace_bind(ws.pid, OWNER, ws.paper_id, "research", bad)


def test_listing_is_bounded_and_reports_version_state(ws: LatexEnv) -> None:
    (ws.folder / ".git").mkdir()
    (ws.folder / ".git" / "config").write_text("[core]\n", "utf-8")
    (ws.folder / "main.aux").write_text("relax", "utf-8")
    listing = ws.svc.workspace_files(ws.pid, OWNER, ws.paper_id)
    files = {f["path"]: f for f in listing["files"]}
    assert set(files) == {
        "figures/plot.png",
        "main.aux",
        "main.tex",
        "refs.bib",
        "sections/intro.tex",
    }
    assert listing["skipped"]["hidden"] == 1
    assert files["main.tex"]["state"] == "versioned"
    assert files["refs.bib"]["state"] == "unversioned"
    assert files["main.aux"]["compile_input"] is False
    assert files["figures/plot.png"]["text"] is False
    assert (
        files["main.tex"]["sha256"] == hashlib.sha256(ws.path("main.tex").read_bytes()).hexdigest()
    )
    # Listing is pure: no sequence change.
    seq = ws.env.seq()
    ws.svc.workspace_files(ws.pid, AGENT, ws.paper_id)
    assert ws.env.seq() == seq


def test_listing_skips_links_and_reparse_points(ws: LatexEnv, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tex").write_text("secret", "utf-8")
    junction = ws.folder / "linked"
    if os.name == "nt":
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        junction.symlink_to(outside, target_is_directory=True)
    listing = ws.svc.workspace_files(ws.pid, OWNER, ws.paper_id)
    assert not any(f["path"].startswith("linked") for f in listing["files"])
    assert listing["skipped"]["links"] == 1
    with pytest.raises(PrometheosError) as refused:
        ws.read("linked/secret.tex")
    assert refused.value.code == "policy_denied"


def test_reads_are_bounded_and_confined(ws: LatexEnv) -> None:
    read = ws.read("sections/intro.tex")
    assert read["text"].startswith("This synthetic manuscript")
    assert read["line_ending"] == "lf"
    for bad in ("../main.tex", "/etc/passwd", "C:/Windows/win.ini", "sections/../../x.tex"):
        with pytest.raises(PrometheosError) as refused:
            ws.read(bad)
        assert refused.value.code in ("policy_denied", "validation_error")
    ws.path("big.tex").write_bytes(b"%" * (2 * 1024 * 1024 + 1))
    with pytest.raises(PrometheosError) as big:
        ws.read("big.tex")
    assert big.value.code == "budget_exceeded"


def test_write_is_refused_when_the_file_changed_externally(ws: LatexEnv) -> None:
    loaded = ws.read("main.tex")
    draft = loaded["text"].replace("Results", "Findings")
    # Another process edits the file after the editor loaded it.
    ws.path("main.tex").write_text(loaded["text"] + "% external edit\n", "utf-8")
    external = ws.path("main.tex").read_bytes()
    with pytest.raises(PrometheosError) as refused:
        ws.write("main.tex", draft, loaded["sha256"])
    assert refused.value.code == "revision_conflict"
    assert refused.value.details["disk_sha256"] == hashlib.sha256(external).hexdigest()
    assert ws.path("main.tex").read_bytes() == external  # nothing overwritten
    # Reloading gives the new digest; the draft can then be saved deliberately.
    fresh = ws.read("main.tex")
    saved = ws.write("main.tex", draft, fresh["sha256"])
    assert ws.path("main.tex").read_text("utf-8") == draft
    assert saved["sha256"] == hashlib.sha256(draft.encode()).hexdigest()
    assert not list(ws.folder.glob(".*prometheos-tmp"))


def test_write_rules(ws: LatexEnv) -> None:
    created = ws.write("sections/new.tex", "New section.\n", None)
    assert created["created"] is True
    with pytest.raises(PrometheosError) as exists:
        ws.write("sections/new.tex", "again", None)  # creating over an existing file
    assert exists.value.code == "revision_conflict"
    for bad in ("figures/plot.png", ".hidden.tex", ".git/config", "x.exe", "../escape.tex"):
        with pytest.raises(PrometheosError):
            ws.write(bad, "x", None)
    crlf = "line one\r\nline two\r\n"
    base = ws.write("crlf.tex", crlf, None)["sha256"]
    assert ws.read("crlf.tex")["line_ending"] == "crlf"
    assert ws.path("crlf.tex").read_bytes() == crlf.encode()
    assert base == hashlib.sha256(crlf.encode()).hexdigest()


def test_saving_does_not_version_but_snapshot_does(ws: LatexEnv) -> None:
    source_id = ws.paper()["body"]["manuscript_source_id"]
    before = len(_versions(ws, source_id))
    loaded = ws.read("main.tex")
    ws.write("main.tex", loaded["text"] + "% note\n", loaded["sha256"])
    assert len(_versions(ws, source_id)) == before
    snap = ws.svc.workspace_snapshot(ws.pid, OWNER, ws.paper_id)
    assert len(_versions(ws, source_id)) == before + 1
    assert set(snap["versions"]) >= {"main.tex", "refs.bib", "sections/intro.tex"}
    # A second snapshot with nothing changed records nothing.
    again = ws.svc.workspace_snapshot(ws.pid, OWNER, ws.paper_id)
    assert again["commits"] == [] and again["recorded"] == 0
    assert again["versions"]["main.tex"] == snap["versions"]["main.tex"]


def test_snapshot_runs_the_anchor_remap_rules(ws: LatexEnv) -> None:
    source_id = ws.paper()["body"]["manuscript_source_id"]
    version = _versions(ws, source_id)[0]
    lines = ws.path("main.tex").read_text("utf-8").split("\n")
    results = next(n for n, t in enumerate(lines, 1) if "settling time" in t)
    caption = next(n for n, t in enumerate(lines, 1) if "\\caption" in t)
    moved, changed = new_id("anchor"), new_id("anchor")
    ws.env.apply(
        [
            create("anchor", anchor_body(ws.paper_id, version["id"], results), moved),
            create("anchor", anchor_body(ws.paper_id, version["id"], caption), changed),
        ]
    )
    loaded = ws.read("main.tex")
    text = loaded["text"].replace("\\maketitle\n", "\\maketitle\n% inserted line\n")
    text = text.replace("Synthetic response.", "Synthetic response (revised).")
    ws.write("main.tex", text, loaded["sha256"])
    snap = ws.svc.workspace_snapshot(ws.pid, OWNER, ws.paper_id)
    new_version = snap["versions"]["main.tex"]["version_id"]
    anchor = ws.get(moved)["body"]
    assert anchor["manuscript_version_id"] == new_version
    assert anchor["locator"]["start_line"] == results + 1 and anchor["status"] == "current"
    assert ws.get(changed)["body"]["status"] == "needs_review"


def test_structure_query_is_pure_and_complete(ws: LatexEnv) -> None:
    seq = ws.env.seq()
    result = ws.svc.workspace_structure(ws.pid, AGENT, ws.paper_id)
    assert ws.env.seq() == seq
    labels = {item["key"]: item for item in result["labels"]}
    assert set(labels) == {"sec:intro", "sec:results", "fig:plot"}
    assert labels["fig:plot"]["file"] == "main.tex"
    assert {b["key"] for b in result["bib_keys"]} == {"doe2024", "roe2023"}
    cites = {(c["key"], c["file"]) for c in result["cites"]}
    assert ("roe2023", "sections/intro.tex") in cites and ("doe2024", "main.tex") in cites
    assert [o["title"] for o in result["outline"]] == ["Introduction", "Results"]
    assert result["undefined_refs"] == [] and result["undefined_cites"] == []
    assert {i["target"] for i in result["includes"]} >= {"sections/intro", "refs"}


def test_structure_reports_anchor_claims_for_the_gutter(ws: LatexEnv) -> None:
    source_id = ws.paper()["body"]["manuscript_source_id"]
    version = _versions(ws, source_id)[0]
    item = {"source_id": source_id, "version_id": version["id"]}
    lines = ws.path("main.tex").read_text("utf-8").split("\n")
    results = next(n for n, t in enumerate(lines, 1) if "settling time" in t)
    claim_id = new_id("claim")
    scope = {
        "system": "synthetic controller",
        "operating_regime": "simulated regime",
        "comparator": "reference",
        "model_fidelity": "toy",
        "validation_level": "none",
    }
    ws.env.apply(
        [
            create(
                "claim",
                claim_body(
                    "Settling time drops in simulation.", scope, [text_evidence(item, results)]
                ),
                claim_id,
            )
        ]
    )
    anchor_id = new_id("anchor")
    ws.env.apply(
        [
            create(
                "anchor",
                anchor_body(
                    ws.paper_id, version["id"], results, claim_refs=[{"id": claim_id, "rev": 1}]
                ),
                anchor_id,
            )
        ]
    )
    result = ws.svc.workspace_structure(ws.pid, OWNER, ws.paper_id)
    anchor = next(a for a in result["anchors"] if a["id"] == anchor_id)
    assert anchor["anchored_lines"] == [lines[results - 1]]
    assert anchor["claims"][0]["statement"] == "Settling time drops in simulation."


def test_insert_citation_and_figure_from_evidence(ws: LatexEnv) -> None:
    (ws.env.research / "result.png").write_bytes(ws.path("figures/plot.png").read_bytes())
    imported = ws.env.import_files(["baseline.md", "result.png"])
    baseline, figure = imported["plan"]["items"]
    cite = ws.svc.workspace_insert(ws.pid, OWNER, ws.paper_id, "cite", baseline["version_id"])
    assert cite["snippet"].startswith("\\cite{prometheos-") and cite["bib_file"] == "refs.bib"
    bib = ws.path("refs.bib").read_text("utf-8")
    assert cite["key"] in bib and baseline["version_id"] in bib
    assert cite["relation"] is not None
    links = ws.svc.links(ws.pid, OWNER, ws.paper_id)
    assert any(r["source"] == baseline["version_id"] for r in links["relations"])
    again = ws.svc.workspace_insert(ws.pid, OWNER, ws.paper_id, "cite", baseline["version_id"])
    assert again["written"] is False and again["relation"] is None
    assert ws.path("refs.bib").read_text("utf-8") == bib
    fig = ws.svc.workspace_insert(ws.pid, OWNER, ws.paper_id, "figure", figure["version_id"])
    assert fig["file"] == "figures/result.png"
    assert ws.path("figures/result.png").read_bytes() == ws.path("figures/plot.png").read_bytes()
    assert "\\includegraphics" in fig["snippet"] and "figures/result.png" in fig["snippet"]
    with pytest.raises(PrometheosError) as text_figure:
        ws.svc.workspace_insert(ws.pid, OWNER, ws.paper_id, "figure", baseline["version_id"])
    assert text_figure.value.code == "unsupported"


def test_restricted_paper_workspace_needs_owner_scope(ws: LatexEnv) -> None:
    """Decision 0012 item 22 (primary's decision on review 0014 R1-L3): an agent gets the
    same answer for a restricted paper as for an absent one, saying owner scope is needed
    and naming no restricted record; the owner works with it at the owner's rank."""
    import shutil

    from cp7_support import paper_body
    from cp9_support import LATEX_FIXTURE

    secret_paper = new_id("paper")
    ws.env.apply([create("paper", paper_body(), secret_paper, sensitivity="restricted")])
    shutil.copytree(LATEX_FIXTURE, ws.env.research / "secret")
    ws.svc.workspace_bind(ws.pid, OWNER, secret_paper, "research", "secret/main.tex")
    absent = new_id("paper")

    def refusal(paper_id: str, call) -> dict:  # type: ignore[no-untyped-def]
        with pytest.raises(PrometheosError) as refused:
            call(paper_id)
        response = refused.value.to_response().model_dump()
        assert refused.value.code == "not_found"
        assert "owner scope" in refused.value.message
        return {"code": response["error"]["code"], "message": response["error"]["message"]}

    calls = [
        lambda p: ws.svc.workspace_files(ws.pid, AGENT, p),
        lambda p: ws.svc.workspace_read(ws.pid, AGENT, p, "main.tex"),
        lambda p: ws.svc.workspace_structure(ws.pid, AGENT, p),
        lambda p: ws.svc.workspace_snapshot(ws.pid, AGENT, p),
        lambda p: ws.svc.workspace_write(ws.pid, AGENT, p, "x.tex", "x", None),
        lambda p: ws.svc.paper_compile_status(ws.pid, AGENT, p),
        lambda p: ws.svc.workspace_bind(ws.pid, AGENT, p, "research", "secret/main.tex"),
    ]
    seq = ws.env.seq()
    for call in calls:
        hidden = refusal(secret_paper, call)
        missing = refusal(absent, call)
        assert hidden == missing  # indistinguishable from an absent paper
        assert "source_" not in hidden["message"] and "version_" not in hidden["message"]
    assert ws.env.seq() == seq
    assert not (ws.env.research / "secret" / "x.tex").exists()
    # The owner works with the restricted workspace; its versions stay restricted.
    snap = ws.svc.workspace_snapshot(ws.pid, OWNER, secret_paper)
    version = ws.get(snap["versions"]["refs.bib"]["version_id"])
    assert version["sensitivity"] == "restricted"
    listing = ws.svc.workspace_files(ws.pid, OWNER, secret_paper)
    assert {f["state"] for f in listing["files"] if f["compile_input"]} == {"versioned"}
