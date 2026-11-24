"""Identifier normalisation and validation (CP11 S11.2; decision 0062 §2).

Each function returns the normalised value, or raises :class:`Invalid` with a reason that the
plan reports per field. Nothing here looks anything up on the network.

- DOI: lower case; ``https://doi.org/``, ``http://dx.doi.org/`` and ``doi:`` prefixes removed;
  trailing ``,`` and ``;`` and an unbalanced closing ``)`` or ``]`` removed (text around a
  DOI, never part of a registered suffix in practice); a trailing ``.`` is kept, since some
  registered suffixes end in one; the ``10.<registrant>/<suffix>`` form checked.
- arXiv: new style ``YYMM.NNNNN`` and old style ``archive/YYMMNNN``; an ``arXiv:`` prefix, a
  trailing ``[class]`` and an old-style subject class (``math.AP/0601001`` is
  ``math/0601001``) are removed; the version suffix (``v2``) is returned separately.
- PMID: digits only. PMCID: ``PMC`` followed by digits.
- ISBN: ISBN-13 with its check digit validated; an ISBN-10 (check digit validated) is
  converted to ISBN-13.
"""

from __future__ import annotations

import re

IDENTITY_SCHEMES = ("doi", "arxiv", "pmid", "pmcid")
"""Identifiers that make two records the same work (exact dedup)."""


class Invalid(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.|www\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_MANY_DOIS = re.compile(r"(?:\s|[;,]\s*)(?:doi:\s*)?10\.[0-9]{4,9}/", re.IGNORECASE)
_DOI = re.compile(r"^10\.[0-9]{4,9}/[!-~]{1,300}$")


def doi(value: str) -> str:
    text = value.strip()
    url = bool(re.match(r"^https?://", text, re.IGNORECASE))
    text = _DOI_PREFIX.sub("", text).strip()
    if url:
        from urllib.parse import unquote

        text = unquote(re.split(r"[?#]", text, maxsplit=1)[0])
    text = _trim_doi(text)
    if not text:
        raise Invalid("empty DOI")
    if _MANY_DOIS.search(text):
        raise Invalid("more than one DOI")
    if any(ch.isspace() for ch in text):
        raise Invalid("a DOI has no spaces")
    lowered = text.lower()
    if not _DOI.match(lowered) or len(lowered) > 320:
        raise Invalid("not a DOI of the form 10.<registrant>/<suffix>")
    return lowered


def _trim_doi(text: str) -> str:
    """Punctuation that ends the sentence or list around a DOI (review round 2, R2-F5)."""
    while text:
        last = text[-1]
        if last in ",;":
            text = text[:-1]
        elif last in ")]" and text.count(last) > text.count("(" if last == ")" else "["):
            text = text[:-1]
        else:
            break
    return text


_ARXIV_NEW = re.compile(r"^(?:arxiv:\s*)?([0-9]{4}\.[0-9]{4,5})(v[0-9]{1,3})?$", re.IGNORECASE)
_ARXIV_OLD = re.compile(
    r"^(?:arxiv:\s*)?([a-z][a-z-]{1,30})(?:\.[a-z][a-z-]{1,10})?/([0-9]{7})(v[0-9]{1,3})?$",
    re.IGNORECASE,
)
_ARXIV_CLASS = re.compile(r"\s*\[[a-z][a-z.-]{1,30}\]$", re.IGNORECASE)
_ARXIV_URL = re.compile(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", re.IGNORECASE)


def arxiv(value: str) -> tuple[str, str | None]:
    """``(identifier without version, version or None)``."""
    text = _ARXIV_URL.sub("", value.strip()).removesuffix(".pdf").strip()
    text = _ARXIV_CLASS.sub("", text)  # ``arXiv:2101.00001 [math.AP]``
    match = _ARXIV_NEW.match(text)
    if match is not None:
        identifier, number, version = match.group(1), match.group(1), match.group(2)
    else:
        match = _ARXIV_OLD.match(text)
        if match is not None:
            # The subject class is not part of an old-style identifier (math.AP/0601001 is
            # math/0601001; review round 2, R2-F5).
            number, version = match.group(2), match.group(3)
            identifier = f"{match.group(1)}/{number}"
    if match is not None:
        if not 1 <= int(number[2:4]) <= 12:
            raise Invalid("not an arXiv identifier (month out of range)")
        return identifier.lower(), version.lower() if version else None
    raise Invalid("not an arXiv identifier (YYMM.NNNNN or archive/YYMMNNN)")


def pmid(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^pmid:\s*", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", text):
        raise Invalid("a PMID is 1 to 10 digits")
    return text


def pmcid(value: str) -> str:
    text = value.strip().upper().replace(" ", "")
    if re.fullmatch(r"[1-9][0-9]{0,9}", text):
        raise Invalid("a PMCID starts with PMC")
    if not re.fullmatch(r"PMC[1-9][0-9]{0,9}", text):
        raise Invalid("a PMCID is PMC followed by digits")
    return text


def _isbn13_check(first12: str) -> str:
    total = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def isbn(value: str) -> str:
    """An ISBN-13 (an ISBN-10 is converted)."""
    text = re.sub(r"^isbn(?:-1[03])?:?\s*", "", value.strip(), flags=re.IGNORECASE)
    digits = re.sub(r"[\s-]", "", text).upper()
    if re.fullmatch(r"[0-9]{9}[0-9X]", digits):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        if total % 11 != 0:
            raise Invalid("ISBN-10 check digit does not match")
        first12 = "978" + digits[:9]
        return first12 + _isbn13_check(first12)
    if re.fullmatch(r"97[89][0-9]{10}", digits):
        if _isbn13_check(digits[:12]) != digits[12]:
            raise Invalid("ISBN-13 check digit does not match")
        return digits
    raise Invalid("not an ISBN-10 or ISBN-13")
