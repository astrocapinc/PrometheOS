"""C09-COMPILE (S19) against the real pinned Tectonic and real stores.

A paper bound to a multi-file LaTeX folder with a .bib file and one figure compiles; an
undefined reference and an error are introduced, compiled, fixed and recompiled.
Diagnostics name file and line; the failing compile keeps the previous PDF labelled with
its older version; every PDF is a generated version bound to the exact input versions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from cp9_support import LatexEnv

pytestmark = pytest.mark.tectonic


@pytest.fixture
def ws(tmp_path: Path, tectonic_engine: Path) -> LatexEnv:
    return LatexEnv(Env(tmp_path))


def _versions_by_path(ws: LatexEnv, result: dict[str, Any]) -> dict[str, str]:
    return dict(result["inputs"])


def test_compile_registers_outputs_bound_to_exact_inputs(ws: LatexEnv) -> None:
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "succeeded", result["diagnostics"]
    assert set(result["outputs"]) == {"pdf", "log", "synctex"}
    inputs = _versions_by_path(ws, result)
    assert set(inputs) == {"main.tex", "sections/intro.tex", "refs.bib", "figures/plot.png"}
    pdf = ws.get(result["outputs"]["pdf"])
    body = pdf["body"]
    assert body["mime"] == "application/pdf" and body["availability"] == "retained"
    transformation = body["transformation"]
    assert transformation["type"] == "conversion"
    assert transformation["engine"] == "tectonic"
    assert transformation["engine_version"] == "0.17.0"
    assert transformation["input_version_refs"] == sorted(inputs.values())
    # Each input version holds the exact bytes on disk at compile time.
    for rel, version_id in inputs.items():
        version = ws.get(version_id)
        import hashlib

        assert version["body"]["sha256"] == hashlib.sha256(ws.path(rel).read_bytes()).hexdigest()
    pdf_bytes, media = ws.svc.version_content(ws.pid, OWNER, result["outputs"]["pdf"])
    assert media == "application/pdf" and pdf_bytes.startswith(b"%PDF-")
    # The bibliography was processed: no undefined-citation warning remains.
    assert not [d for d in result["diagnostics"] if "Citation" in d["message"]]
    # A second compile with unchanged inputs records no new input versions.
    again = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert again["status"] == "succeeded"
    assert again["inputs"] == result["inputs"]


def test_failed_compile_keeps_previous_pdf_and_maps_diagnostics(ws: LatexEnv) -> None:
    first = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert first["status"] == "succeeded"
    main = ws.read("main.tex")
    broken = main["text"].replace("Figure~\\ref{fig:plot}", "Figure~\\ref{fig:missing}")
    ws.write("main.tex", broken, main["sha256"])
    warned = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert warned["status"] == "succeeded"
    undefined = [d for d in warned["diagnostics"] if "fig:missing" in d["message"]]
    assert undefined and undefined[0]["file"] == "main.tex"
    line = next(n for n, text in enumerate(broken.split("\n"), 1) if "fig:missing" in text)
    assert undefined[0]["line"] == line and undefined[0]["severity"] == "warning"
    # Now a real error in the second file.
    intro = ws.read("sections/intro.tex")
    ws.write("sections/intro.tex", intro["text"] + "\\undefinedcommandxyz\n", intro["sha256"])
    failed = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert failed["status"] == "failed"
    assert "pdf" not in failed["outputs"] and "log" in failed["outputs"]
    errors = [d for d in failed["diagnostics"] if d["severity"] == "error"]
    assert errors[0]["file"] == "sections/intro.tex"
    assert errors[0]["line"] == len(intro["text"].split("\n"))
    status = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    assert status["last_compile"]["status"] == "failed"
    assert status["last_compile"]["pdf_current"] is False
    # The PDF on show is the previous one, with its own (older) inputs.
    assert status["pdf"]["version_id"] == warned["outputs"]["pdf"]
    assert status["pdf"]["transformation"]["input_version_refs"] == sorted(
        warned["inputs"].values()
    )
    assert status["last_compile"]["diagnostics"][0]["file"] == "sections/intro.tex"
    # Fix and recompile: a new PDF bound to the fixed inputs.
    fixed = ws.read("sections/intro.tex")
    ws.write("sections/intro.tex", intro["text"], fixed["sha256"])
    main2 = ws.read("main.tex")
    ws.write("main.tex", main["text"], main2["sha256"])
    final = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert final["status"] == "succeeded"
    assert final["outputs"]["pdf"] != warned["outputs"]["pdf"]
    after = ws.svc.paper_compile_status(ws.pid, OWNER, ws.paper_id)
    assert after["last_compile"]["pdf_current"] is True
    assert after["pdf"]["version_id"] == final["outputs"]["pdf"]


def test_synctex_forward_and_inverse_round_trip(ws: LatexEnv) -> None:
    result = ws.svc.paper_compile(ws.pid, OWNER, ws.paper_id)
    assert result["status"] == "succeeded"
    text = ws.read("main.tex")["text"].split("\n")
    line = next(n for n, t in enumerate(text, 1) if "settling time" in t)
    forward = ws.svc.synctex_forward(ws.pid, OWNER, ws.paper_id, "main.tex", line)
    target = forward["target"]
    assert target is not None and target["page"] == 1 and target["rects"]
    rect = target["rects"][0]
    back = ws.svc.synctex_inverse(
        ws.pid,
        OWNER,
        ws.paper_id,
        target["page"],
        rect["x"] + 5,
        rect["y"] + rect["height"] / 2,
    )
    assert back["source"] == {"file": "main.tex", "line": line}
    intro_fwd = ws.svc.synctex_forward(ws.pid, OWNER, ws.paper_id, "sections/intro.tex", 2)
    assert intro_fwd["target"] is not None
    assert intro_fwd["target"]["file"] == "sections/intro.tex"
    # The stored map holds workspace-relative paths only (no local staging path).
    sync_id = result["outputs"]["synctex"]
    sync_text = ws.svc.version_text(ws.pid, OWNER, sync_id)
    assert sync_text["text"] is None or "staging" not in sync_text["text"]


def test_agent_scope_can_compile_a_normal_workspace(ws: LatexEnv) -> None:
    result = ws.svc.paper_compile(ws.pid, AGENT, ws.paper_id)
    assert result["status"] == "succeeded"


def test_restricted_paper_compiles_only_at_owner_scope(
    tmp_path: Path, tectonic_engine: Path
) -> None:
    """Decision 0012 item 22: the owner compiles a restricted paper (outputs stay
    restricted); an agent gets the same answer as for an absent paper."""
    import shutil

    from cp7_support import create, paper_body
    from cp9_support import LATEX_FIXTURE

    from prometheos.domain.ids import new_id
    from prometheos.domain.models import PrometheosError

    env = Env(tmp_path)
    paper = new_id("paper")
    env.apply([create("paper", paper_body(), paper, sensitivity="restricted")])
    shutil.copytree(LATEX_FIXTURE, env.research / "secret")
    env.svc.workspace_bind(env.project_id, OWNER, paper, "research", "secret/main.tex")
    with pytest.raises(PrometheosError) as refused:
        env.svc.paper_compile(env.project_id, AGENT, paper)
    assert refused.value.code == "not_found" and "owner scope" in refused.value.message
    result = env.svc.paper_compile(env.project_id, OWNER, paper)
    assert result["status"] == "succeeded"
    pdf = env.svc.get(env.project_id, OWNER, result["outputs"]["pdf"], None)
    assert pdf["sensitivity"] == "restricted"
