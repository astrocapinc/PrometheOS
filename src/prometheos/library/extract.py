"""PDF inspection for the library importer (CP11 S11.1; decisions 0060 and 0061).

``inspect_pdf(data, sha)`` checks the bytes first (a PDF header, an end-of-file marker, the size
limit), then extracts per-page text in a separate process (``extract_worker``) with a time limit,
normalises it and returns either the ``pdf_pages_v1`` payload with the detected metadata or a
refusal with a reason. The same bytes always give the same payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.library import contracts as c
from prometheos.library import text as t

WORKER = Path(__file__).resolve().parent / "extract_worker.py"
EXTRACT_SECONDS = 120.0
HEADER_WINDOW = 1024
EOF_WINDOW = 2048

REASONS: dict[str, str] = {
    "not_pdf": "not a PDF (no %PDF- header): a file named .pdf with other content",
    "truncated": "truncated or incomplete (no %%EOF marker at the end)",
    "encrypted": "encrypted: the PDF needs a password, which PrometheOS never asks for",
    "malformed": "malformed: PDFium could not read the file",
    "too_many_pages": "too many pages (page-count limit)",
    "no_pages": "the PDF has no pages",
    "too_large": "larger than the import limit",
    "text_too_large": "its extracted text is larger than the 8 MiB inline limit",
    "extract_failed": "the extraction process failed or timed out (isolated; nothing else ran)",
    "link": "a link or junction: links inside research roots are never followed",
    "unreadable": "could not be read",
    "empty": "an empty file",
    "placeholder": "a cloud placeholder: it is not downloaded (never opened)",
    "owner_scope": "bound to a record outside this scope: importing it needs owner scope",
    "reference_only": "bound to a reference-only source: library import keeps a copy",
}


@dataclass(frozen=True)
class Refusal:
    reason: str
    detail: str = ""

    def view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail or REASONS.get(self.reason, "")}


@dataclass
class Inspected:
    """A readable PDF: its payload (``pdf_pages_v1``) and what page 1 and the info dictionary
    say. ``payload_bytes`` is the canonical JSON that becomes the extracted version."""

    payload: dict[str, Any]
    payload_bytes: bytes
    title: t.Span | None  # from page 1 (offsets), when the info title is not usable
    info_title: str | None
    authors: list[str]
    doi: t.Span | None
    arxiv: t.Span | None
    pages_with_text: int
    pages_without_text: list[int] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return int(self.payload["page_count"])

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()


def extraction_parameters() -> dict[str, Any]:
    """What the extracted text depends on besides the bytes and the engine (its digest is the
    transformation's ``parameters_digest``)."""
    return {
        "normalisation": c.NORMALISATION,
        "max_pages": c.MAX_PAGES,
        "max_page_chars": c.MAX_PAGE_CHARS,
    }


def parameters_digest() -> str:
    return canonical_sha256(extraction_parameters())


def precheck(data: bytes, limit: int) -> Refusal | None:
    """What the bytes alone decide, before any parser sees them."""
    if not data:
        return Refusal("empty")
    if len(data) > limit:
        return Refusal("too_large", f"{len(data)} bytes; the limit is {limit}")
    if b"%PDF-" not in data[:HEADER_WINDOW]:
        return Refusal("not_pdf")
    if b"%%EOF" not in data[-EOF_WINDOW:]:
        return Refusal("truncated")
    return None


def max_total_chars() -> int:
    """The child's cumulative cap (review F2). Raw characters at most halve under normalisation
    (CR LF to LF) and each stored character takes at least one byte, so more than twice the
    payload limit can never fit: a file over it is certainly ``text_too_large``."""
    return 2 * c.MAX_TEXT_BYTES


def _run_worker(data: bytes) -> dict[str, Any]:
    env = {
        k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "TEMP", "TMP")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                str(WORKER),
                "--max-pages",
                str(c.MAX_PAGES),
                "--max-chars",
                str(c.MAX_PAGE_CHARS),
                "--max-total-chars",
                str(max_total_chars()),
            ],
            input=data,
            capture_output=True,
            timeout=EXTRACT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "extract_failed", "detail": "timed out"}
    except OSError as exc:
        return {"ok": False, "reason": "extract_failed", "detail": type(exc).__name__}
    if done.returncode != 0:
        return {
            "ok": False,
            "reason": "extract_failed",
            "detail": f"the extraction process ended with code {done.returncode}",
        }
    try:
        reply = json.loads(done.stdout.decode("utf-8", "surrogatepass"))
    except (UnicodeDecodeError, ValueError):
        return {"ok": False, "reason": "extract_failed", "detail": "unreadable reply"}
    if not isinstance(reply, dict):
        return {"ok": False, "reason": "extract_failed", "detail": "unreadable reply"}
    return reply


def _page(number: int, raw: Any, state: str | None) -> dict[str, Any]:
    if state == "too_long" or not isinstance(raw, str):
        text = ""
        state = "too_long"
    else:
        text = t.normalise_page_text(raw)
        if len(text) > c.MAX_PAGE_CHARS:
            text, state = "", "too_long"
        else:
            state = "text" if text else "no_text"
    return {
        "page": number,
        "state": state,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def inspect_pdf(data: bytes, sha256: str, limit: int = c.MAX_PDF_BYTES) -> Inspected | Refusal:
    refused = precheck(data, min(limit, c.MAX_PDF_BYTES))
    if refused is not None:
        return refused
    reply = _run_worker(data)
    if reply.get("ok") is not True:
        reason = str(reply.get("reason", "extract_failed"))
        if reason not in REASONS:
            reason = "extract_failed"
        return Refusal(reason, str(reply.get("detail", ""))[:300])
    try:
        raw_pages = list(reply["pages"])
        engine = dict(reply["engine"])
        info_raw = dict(reply["info"])
        count = int(reply["page_count"])
    except (KeyError, TypeError, ValueError):
        return Refusal("extract_failed", "unreadable reply")
    if count != len(raw_pages):
        return Refusal("extract_failed", "page count and pages disagree")
    pages = [
        _page(i, p.get("raw"), p.get("state")) if isinstance(p, dict) else _page(i, None, None)
        for i, p in enumerate(raw_pages, start=1)
    ]
    info = {
        key: t.clean_info(info_raw.get(key))
        for key in ("title", "author", "subject", "keywords", "creator", "producer")
    }
    payload = {
        "schema_version": "pdf_pages_v1",
        "pdf_sha256": sha256,
        "engine": {
            "name": "pypdfium2",
            "version": str(engine.get("version", ""))[:40],
            "pdfium": str(engine.get("pdfium", ""))[:40],
        },
        "normalisation": c.NORMALISATION,
        "page_count": count,
        "info": info,
        "pages": pages,
    }
    c.PdfPagesV1.model_validate(payload)  # the contract holds before anything is stored
    payload_bytes = dumps_canonical(payload)
    if len(payload_bytes) > c.MAX_TEXT_BYTES:
        return Refusal("text_too_large")
    first = pages[0]["text"]
    info_title = t.usable_info_title(info["title"])
    return Inspected(
        payload=payload,
        payload_bytes=payload_bytes,
        title=None if info_title else t.first_line_title(first),
        info_title=info_title,
        authors=t.split_authors(info["author"]),
        doi=t.find_doi(first),
        arxiv=t.find_arxiv(first),
        pages_with_text=sum(1 for p in pages if p["state"] == "text"),
        pages_without_text=[p["page"] for p in pages if p["state"] != "text"],
    )


def engine_label(payload: dict[str, Any]) -> str:
    engine = payload["engine"]
    return f"{engine['version']} (PDFium {engine['pdfium']})"[:100]
