"""Pure CP9 helpers: structure scan, hostile-source refusal, read audit, diagnostics,
SyncTeX geometry and remote URL rules. The engine-produced samples below were captured
from the pinned Tectonic 0.17.0 on the synthetic fixture (decision 0012 items 11-13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prometheos.app import gitsync
from prometheos.app.texcompile import audit_reads
from prometheos.domain.models import PrometheosError
from prometheos.latex import diagnostics, structure, synctex


def test_comment_stripping_respects_escaped_percent() -> None:
    assert structure.strip_comment("50\\% done % note") == "50\\% done "
    assert structure.strip_comment("a\\\\% comment") == "a\\\\"


@pytest.mark.parametrize(
    "source",
    [
        "\\immediate\\write18{rm -rf x}",
        "\\write 18{ls}",
        "\\ShellEscape{ls}",
        "\\input{/etc/passwd}",
        "\\input{C:/Windows/win.ini}",
        "\\input{../secret}",
        "\\include{chapters/../../secret}",
        "\\input ../secret ",
        "\\input /etc/passwd",
        "\\includegraphics[width=2cm]{~/photo.png}",
        "\\bibliography{refs,../../other}",
        "\\openin\\handle=/etc/hosts",
        "\\usepackage{../evil}",
    ],
)
def test_hostile_sources_are_refused(source: str) -> None:
    findings = structure.scan_hostile({"main.tex": f"intro\n{source}\n"})
    assert findings and findings[0]["line"] == 2 and findings[0]["file"] == "main.tex"


@pytest.mark.parametrize(
    "source",
    [
        "\\input{sections/intro}",
        "\\includegraphics{figures/plot.png}",
        "\\write16{a message}",
        "\\write182",  # not stream 18
        "% \\write18{commented out}",
        "\\input{\\dir/file}",  # a macro argument is not a literal path (audited after run)
        "See https://example.org/a/../b in text.",
    ],
)
def test_ordinary_sources_pass_the_scan(source: str) -> None:
    assert structure.scan_hostile({"main.tex": source + "\n"}) == []


def test_bib_files_are_not_scanned_for_commands() -> None:
    assert structure.scan_hostile({"refs.bib": "@misc{a, note={\\input{/etc/passwd}}}\n"}) == []


def test_audit_maps_tectonic_makefile_paths(tmp_path: Path) -> None:
    stage = tmp_path / "work"
    out = tmp_path / "out"
    stage.mkdir()
    out.mkdir()
    clean = (
        f"{out}\\main.pdf {out}\\main.log : main.tex \\\n  {out}\\refs.bib \\\n"
        f"  {out}\\sections/intro.tex \\\n  {out}\\figures/plot.png\n"
    )
    assert audit_reads("", clean, "", stage, out) == []
    escaped = clean.replace(f"{out}\\refs.bib", f"{out}\\../../planted.tex")
    found = audit_reads("", escaped, "", stage, out)
    assert [f["name"] for f in found] == ["planted.tex"]
    absolute = clean.replace(f"{out}\\refs.bib", "C:/Users/someone/secret.tex")
    assert [f["name"] for f in audit_reads("", absolute, "", stage, out)] == ["secret.tex"]
    warning = "warning: accessing absolute path `C:/x/secret.tex`; build may not be reproducible"
    assert [f["name"] for f in audit_reads(warning, None, "", stage, out)] == ["secret.tex"]
    log = "(main.tex (../../outside.tex) )"
    assert [f["name"] for f in audit_reads("", None, log, stage, out)] == ["outside.tex"]


ENGINE_SAMPLE = """Fontconfig error: Cannot load default config file: No such file: (null)\r
error: main.tex:15: Undefined control sequence
error: something bad happened inside XeTeX; its output follows:

===============================================================================
(main.tex
! Undefined control sequence.
l.15 Figure~\\ref{fig:missing} \\undefinedmacro
===============================================================================
error: the XeTeX engine had an unrecoverable error
caused by: halted on potentially-recoverable error as specified
"""

LOG_SAMPLE = """This is XeTeX, Version 3.141592653-2.6-0.999994 (Tectonic)
(main.tex
LaTeX2e <2021-11-15> patch level 1
(article.cls
Document Class: article 2021/10/04 v1.4n Standard LaTeX document class
(size10.clo))
(sections/intro

LaTeX Warning: Citation `roe2023' on page 1 undefined on input line 2.

)

LaTeX Warning: Reference `fig:missing' on page 1 undefined on input line 15.

! Undefined control sequence.
l.15 Figure~\\ref{fig:missing} \\undefinedmacro
)
"""


def compose(engine: str, log: str, inputs: str = '["main.tex", "sections/intro.tex"]') -> str:
    return (
        "PrometheOS compile log: Tectonic 0.17.0, status failed\n"
        f"inputs: {inputs}\n{diagnostics.ENGINE_HEADER}\n{engine}\n"
        f"{diagnostics.LOG_HEADER} (main.log)\n{log}"
    )


def test_diagnostics_map_to_workspace_file_and_line() -> None:
    found = diagnostics.diagnostics(compose(ENGINE_SAMPLE, LOG_SAMPLE))
    errors = [d for d in found if d["severity"] == "error"]
    assert errors == [
        {
            "severity": "error",
            "file": "main.tex",
            "line": 15,
            "message": "Undefined control sequence",
            "origin": "engine",
            "context": "",
        }
    ]
    warnings = {
        (d["file"], d["line"], d["message"][:24]) for d in found if d["severity"] == "warning"
    }
    assert ("sections/intro.tex", 2, "Citation `roe2023' on pa") in warnings
    assert ("main.tex", 15, "Reference `fig:missing' ") in warnings


def test_diagnostics_drop_files_that_are_not_inputs() -> None:
    engine = "error: article.cls:42: Something inside a package\n"
    found = diagnostics.diagnostics(compose(engine, ""))
    assert found[0]["file"] is None and found[0]["line"] is None


def test_engine_messages_strip_double_bang() -> None:
    engine = "error: main.tex:10: ! LaTeX Error: File `missing.tex' not found.\n"
    found = diagnostics.diagnostics(compose(engine, ""))
    assert found[0]["message"].startswith("LaTeX Error: File")


SYNC = """SyncTeX Version:1
Input:1:main.tex
Input:2:
Input:3:sections/intro.tex
Output:pdf
Magnification:1000
Unit:1
X Offset:0
Y Offset:0
Content:
{1
[1,1:4736286,47362867:30000000,40000000,0
(1,10:4736286,9867264:30000000,655360,131072
x1,10:5000000,9867264
k1,10:9000000,9867264:100000
)
(3,2:4736286,19734528:30000000,655360,131072
x3,2:6000000,19734528
)
]
}1
{2
(1,40:4736286,9867264:30000000,655360,131072
x1,40:5000000,9867264
)
}2
"""


def test_synctex_forward_and_inverse_geometry() -> None:
    sync = synctex.parse(SYNC)
    assert sync.pages == 2 and sync.inputs[3] == "sections/intro.tex"
    forward = synctex.forward(sync, "main.tex", 10)
    assert forward is not None and forward["page"] == 1
    rect = forward["rects"][0]
    assert rect["x"] == pytest.approx(72.0, abs=0.1)  # 1in margin
    assert rect["y"] == pytest.approx(150.0 - 10.0, abs=0.2)
    # A line with no node maps to the next line that has one.
    assert synctex.forward(sync, "main.tex", 11)["line"] == 40  # type: ignore[index]
    assert synctex.forward(sync, "./sections/intro.tex", 2)["page"] == 1  # type: ignore[index]
    assert synctex.inverse(sync, 1, 90.0, 148.0) == {"file": "main.tex", "line": 10}
    assert synctex.inverse(sync, 1, 100.0, 298.0) == {"file": "sections/intro.tex", "line": 2}
    assert synctex.inverse(sync, 2, 90.0, 148.0) == {"file": "main.tex", "line": 40}
    assert synctex.inverse(sync, 3, 90.0, 148.0) is None


def test_synctex_inputs_are_made_workspace_relative() -> None:
    raw = (
        "Input:1:C:\\stage\\work\\main.tex\nInput:2:\nInput:3:C:\\stage\\work\\sections/intro.tex\n"
    )
    raw += "Input:4:D:\\elsewhere\\secret.tex\n"
    text = synctex.normalize_inputs(raw, "C:\\stage\\work")
    assert text.split("\n") == [
        "Input:1:main.tex",
        "Input:2:",
        "Input:3:sections/intro.tex",
        "Input:4:outside:secret.tex",
        "",
    ]


@pytest.mark.parametrize(
    "url",
    ["https://git.overleaf.com/0123456789abcdef", "https://git.overleaf.com/project/abc"],
)
def test_overleaf_urls_without_credentials_are_accepted(url: str) -> None:
    assert gitsync.check_remote_url(url) == url


def test_refusal_message_never_echoes_the_url() -> None:
    with pytest.raises(PrometheosError) as refused:
        gitsync.check_remote_url("https://user:s3cr3t-token@git.overleaf.com/x")
    assert "s3cr3t" not in str(refused.value.to_response().model_dump())
    assert gitsync.redact("fatal: https://user:pw@host/x failed") == "fatal: https://host/x failed"


SOURCE = "\\section{Results}\\label{sec:results} see \\ref{fig:missing} \\cite{doe2024}"


def _d(message: str, file: str | None = "main.tex", severity: str = "error") -> dict:  # type: ignore[type-arg]
    return {
        "severity": severity,
        "file": file,
        "line": 3 if file else None,
        "message": message,
        "origin": "engine",
        "context": "",
    }


def test_sanitize_keeps_templates_that_quote_workspace_text() -> None:
    kept = diagnostics.sanitize(
        [
            _d("Undefined control sequence"),
            _d("Reference `fig:missing' on page 1 undefined on input line 3.", severity="warning"),
            _d("compile stopped after 5 s (time limit)", file=None),
        ],
        {"main.tex"},
        SOURCE,
    )
    assert [k["message"] for k in kept] == [
        "Undefined control sequence",
        "Reference `fig:missing' on page 1 undefined on input line 3.",
        "compile stopped after 5 s (time limit)",
    ]
    assert kept[0]["file"] == "main.tex" and kept[0]["line"] == 3


def test_sanitize_withholds_anything_else() -> None:
    kept = diagnostics.sanitize(
        [
            _d("SECRETCONTENT from a file"),  # \errmessage{<outside text>}
            _d("Reference `SECRETKEY' on page 1 undefined on input line 3."),  # not in sources
            _d("LaTeX Error: File `../../planted.tex' not found."),
            _d("Undefined control sequence", file="../outside.tex"),
            _d("Overfull \\hbox (1.0pt too wide) SECRET", severity="info"),
        ],
        {"main.tex"},
        SOURCE,
    )
    text = str(kept)
    assert "SECRET" not in text and "planted" not in text and "outside" not in text
    # Three withheld errors on the same line collapse into one; box messages are dropped.
    assert [(k["file"], k["line"], k["message"][:17]) for k in kept] == [
        ("main.tex", 3, "details withheld:"),
        (None, None, "Undefined control"),
    ]


def test_sanitized_log_parses_back_to_the_same_diagnostics() -> None:
    kept = diagnostics.sanitize(
        [_d("Undefined control sequence"), _d("free text", file=None)], {"main.tex"}, SOURCE
    )
    log = diagnostics.sanitized_log(
        ["PrometheOS compile log: status failed", 'inputs: ["main.tex"]'], kept
    )
    again = diagnostics.diagnostics(log)
    assert [(d["file"], d["line"], d["message"]) for d in again] == [
        (k["file"], k["line"], k["message"]) for k in kept
    ]
