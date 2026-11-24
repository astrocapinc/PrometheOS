"""Contracts of the library (CP11 S11.1; decision 0060 §3, decision 0061).

``PdfPagesV1`` is the payload of an extracted-text version (``payload_schema=pdf_pages_v1``):
the page text of one PDF version, page by page, each with its character count and SHA-256.
A PDF locator's ``start``/``end`` offsets count Unicode code points of one page's ``text``
(end exclusive); a quotation's digest is the SHA-256 of ``text[start:end]`` encoded as UTF-8.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from prometheos.domain.models import Sha256, StrictModel

MAX_PAGES = 2000
"""A PDF with more pages is refused (page-count bomb); its text is never extracted."""
MAX_PAGE_CHARS = 100_000
"""A page with more extracted characters is kept as ``too_long`` without text."""
MAX_PDF_BYTES = 256 * 1024 * 1024
"""A larger file is refused (below the project's own media limit when that is lower)."""
MAX_TEXT_BYTES = 8 * 1024 * 1024
"""The extracted payload is stored inline; a larger one is refused in S11.1."""
NORMALISATION = "lf-controls-v1"
"""CR LF and CR become LF; other C0 controls except tab, and U+FFFE/U+FFFF, are removed."""

InfoText = Annotated[str, StringConstraints(max_length=1000)]


class PdfInfo(StrictModel):
    """The PDF information dictionary as text (untrusted; never instructions)."""

    title: InfoText | None
    author: InfoText | None
    subject: InfoText | None
    keywords: InfoText | None
    creator: InfoText | None
    producer: InfoText | None


class PdfEngine(StrictModel):
    name: Literal["pypdfium2"]
    version: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    pdfium: Annotated[str, StringConstraints(min_length=1, max_length=40)]


class PdfPage(StrictModel):
    page: int = Field(ge=1, le=MAX_PAGES)
    state: Literal["text", "no_text", "too_long"]
    chars: int = Field(
        ge=0,
        le=MAX_PAGE_CHARS,
        description="Length of text in Unicode code points (offsets count these)",
    )
    sha256: Sha256
    text: Annotated[str, StringConstraints(max_length=MAX_PAGE_CHARS)]

    @model_validator(mode="after")
    def _consistent(self) -> PdfPage:
        if self.chars != len(self.text):
            raise ValueError("chars is the length of text")
        if self.sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("sha256 is the digest of text")
        if (self.state == "text") != bool(self.text):
            raise ValueError("only a text page has text")
        return self


class PdfPagesV1(StrictModel):
    schema_version: Literal["pdf_pages_v1"]
    pdf_sha256: Sha256
    engine: PdfEngine
    normalisation: Literal["lf-controls-v1"]
    page_count: int = Field(ge=1, le=MAX_PAGES)
    info: PdfInfo
    pages: list[PdfPage] = Field(min_length=1, max_length=MAX_PAGES)

    @model_validator(mode="after")
    def _numbered(self) -> PdfPagesV1:
        if [p.page for p in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("pages are numbered 1..page_count in order")
        return self
