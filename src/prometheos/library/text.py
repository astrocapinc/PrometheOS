"""Page text normalisation and first-page metadata detection (CP11 S11.1; decision 0060).

Everything here reads untrusted text and returns spans of it; nothing is interpreted as an
instruction. Offsets are Unicode code points of the normalised page text, end exclusive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def normalise_page_text(raw: str) -> str:
    """``lf-controls-v1``: CR LF and CR become LF, other C0 controls (except tab and LF), DEL,
    lone surrogates and the noncharacters U+FFFE/U+FFFF are removed, and trailing whitespace
    of the page is dropped. The result is what offsets and quotation digests refer to."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROLS.sub("", text).rstrip()


def clean_info(value: object) -> str | None:
    """An information-dictionary string as stored: the same control characters as page text
    removed, whitespace collapsed to single spaces, at most 1,000 characters (review F3)."""
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROLS.sub("", value).split())
    return text[:1000] or None


@dataclass(frozen=True)
class Span:
    value: str  # the normalised value
    start: int
    end: int


# A DOI as printed: 10.<registrant>/<suffix>, the suffix ending before whitespace; trailing
# punctuation that usually closes a sentence or a bracket is not part of it.
_DOI = re.compile(r"\b(10\.[0-9]{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_DOI_TRAIL = ".,;:)]}'"
_ARXIV_NEW = re.compile(r"\barXiv:\s?([0-9]{4}\.[0-9]{4,5})(v[0-9]+)?\b", re.IGNORECASE)
_ARXIV_OLD = re.compile(r"\barXiv:\s?([a-z][a-z.-]{1,30}/[0-9]{7})(v[0-9]+)?\b", re.IGNORECASE)


def find_doi(text: str) -> Span | None:
    for match in _DOI.finditer(text):
        value = match.group(1).rstrip(_DOI_TRAIL)
        if len(value) > 320 or "/" not in value[3:] or value.endswith("/"):
            continue
        if not all("!" <= ch <= "~" for ch in value):
            continue
        start = match.start(1)
        return Span(value.lower(), start, start + len(value))
    return None


def find_arxiv(text: str) -> Span | None:
    for pattern in (_ARXIV_NEW, _ARXIV_OLD):
        match = pattern.search(text)
        if match is not None:
            end = match.end(2) if match.group(2) else match.end(1)
            # An old-style subject class is not part of the identifier (math.AP/0601001 is
            # math/0601001), as for a bibliography field (review round 2, R2-F5).
            value = re.sub(r"\.[a-z][a-z-]*(?=/)", "", match.group(1).lower())
            return Span(value, match.start(1), end)
    return None


_LETTERS = re.compile(r"[A-Za-zÀ-ɏ]")
_HEADING_WORDS = ("abstract", "doi:", "arxiv:", "introduction", "keywords")


def first_line_title(text: str) -> Span | None:
    """The first line of page 1 that reads like a title: at least four letters, at most 300
    characters, not a DOI, arXiv or section heading line. A heuristic with its span recorded."""
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        lead = len(line) - len(line.lstrip())
        if (
            4 <= len(_LETTERS.findall(stripped))
            and len(stripped) <= 300
            and not stripped.lower().startswith(_HEADING_WORDS)
        ):
            start = offset + lead
            return Span(stripped, start, start + len(stripped))
        offset += len(line) + 1
        if offset > 4000:
            break
    return None


_JUNK_TITLES = re.compile(
    r"^(untitled|title|document\d*|microsoft word\b.*|.*\.(docx?|tex|pdf|dvi|ps|rtf|odt))$",
    re.IGNORECASE,
)


def usable_info_title(value: str | None) -> str | None:
    """The information-dictionary title when it looks like one (not empty, not a file name or
    a word-processor placeholder)."""
    if value is None:
        return None
    text = " ".join(value.split())
    if len(_LETTERS.findall(text)) < 4 or len(text) > 300 or _JUNK_TITLES.match(text):
        return None
    return text


_AUTHOR_SPLIT = re.compile(r"\s*(?:;|,\s*and\s+|\s+and\s+|&|\n)\s*")


def split_authors(value: str | None) -> list[str]:
    """Contributor names from an information-dictionary author string (``;``, ``and``, ``&``
    separate names; a comma alone does not, since it often separates family and given name)."""
    if not value:
        return []
    names = []
    for part in _AUTHOR_SPLIT.split(value):
        name = " ".join(part.split()).strip(" ,")
        if 2 <= len(name) <= 300 and _LETTERS.search(name):
            names.append(name)
    return names[:200]
