"""BibTeX, RIS and CSV reading (CP11 S11.2; decisions 0062 §2 and 0063 §1).

Small parsers of our own: they report the line range of every field (the provenance locators),
enforce limits before anything grows (file size, entries, fields, field length, brace depth)
and never interpret a value: LaTeX commands, URLs and embedded instructions stay inert text.
A malformed entry is refused with its line and a reason, and reading goes on with the next
entry; a file that cannot be read at all (binary, not UTF-8, over a limit) is refused whole.

Every format yields :class:`Record` values in one shape: the bibliographic fields a work
takes, each with its line range, the identifiers normalised (``identifiers``), the fields
refused with reasons, and the entry's own key.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from prometheos.library import identifiers as ids

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_FIELDS = 100
MAX_FIELD_CHARS = 20_000
MAX_DEPTH = 32
MAX_EXPANSION = 32 * 1024 * 1024
"""Characters all macro expansions of one BibTeX file may produce (review F6)."""
MAX_TITLE = 300
FORMATS = {".bib": "bibtex", ".ris": "ris", ".csv": "csv"}
MIME = {"bibtex": "application/x-bibtex", "ris": "application/x-research-info-systems",
        "csv": "text/csv"}  # fmt: skip

ENTRY_TYPES = (
    "article", "inproceedings", "book", "incollection", "phdthesis", "mastersthesis",
    "techreport", "misc", "unpublished", "online", "preprint",
)  # fmt: skip
_TYPE_ALIASES = {
    "conference": "inproceedings", "inbook": "incollection", "report": "techreport",
    "electronic": "online", "www": "online", "thesis": "phdthesis",
}  # fmt: skip
_MONTHS = {m: m for m in ("jan feb mar apr may jun jul aug sep oct nov dec".split())}


class FileRefused(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class Field:
    value: str
    start_line: int
    end_line: int
    pointer: str | None = None  # a JSON pointer into a Zotero item (S11.5; lines are 0 then)


@dataclass
class Record:
    """One bibliography entry in the shape a work takes."""

    key: str
    start_line: int
    end_line: int
    entry_type: str | None = None
    fields: dict[str, Field] = field(default_factory=dict)  # work field -> value and lines
    contributors: list[dict[str, str]] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)  # scheme -> normalised value
    related: dict[str, str] = field(default_factory=dict)  # version_doi / version_arxiv
    arxiv_version: str | None = None
    work_ref: str | None = None  # a PrometheOS work ID (our own export)
    refused_fields: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None  # the entry is refused (malformed, conflicting identifiers...)
    # S11.5 (decision 0070): where the record came from. ``zotero`` records cite JSON pointers
    # into the retained item JSON; their ``key`` is the item key, and only a citationKey the
    # item itself carries is a citation key.
    basis: str = "bibliography"
    citekey: str | None = None


@dataclass
class Parsed:
    format: str
    records: list[Record]
    unmapped_columns: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)


def decode(data: bytes) -> str:
    """The text of a bibliography file, or :class:`FileRefused`."""
    if len(data) > MAX_FILE_BYTES:
        raise FileRefused("too_large", f"{len(data)} bytes; the limit is {MAX_FILE_BYTES}")
    if b"\x00" in data:
        raise FileRefused("binary", "binary content (NUL bytes): not a bibliography text file")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileRefused(
            "not_utf8", f"not UTF-8 (byte {exc.start}); convert the file to UTF-8"
        ) from exc


def clean(value: str) -> str:
    """A field value as stored: protective braces removed (escaped braces kept as braces),
    whitespace collapsed. LaTeX commands are kept as written (inert text)."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in "{}":
            out.append(value[i + 1])
            i += 2
            continue
        if ch in "{}":
            i += 1
            continue
        out.append(ch)
        i += 1
    return " ".join("".join(out).split())


# ----- BibTeX -------------------------------------------------------------------------------------


class _BibError(Exception):
    def __init__(self, reason: str, pos: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pos = pos


class _Bib:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.strings: dict[str, str] = dict(_MONTHS)
        self.expanded = 0  # characters produced by values so far (bounded by MAX_EXPANSION)
        # Offsets where each line starts, for line numbers by bisection.
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, pos: int) -> int:
        import bisect

        return bisect.bisect_right(self.starts, pos)

    def ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def ident(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] in "_-:.+/'"
        ):
            self.pos += 1
        return self.text[start : self.pos]

    def braced(self) -> str:
        """``{...}`` with nested braces (depth limited); returns the inner text."""
        assert self.text[self.pos] == "{"
        depth, start = 0, self.pos
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            if ch == "{":
                depth += 1
                if depth > MAX_DEPTH:
                    raise _BibError(f"braces nested deeper than {MAX_DEPTH}", start)
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return self.text[start + 1 : self.pos - 1]
            self.pos += 1
            if self.pos - start > MAX_FIELD_CHARS:
                raise _BibError(f"a value longer than {MAX_FIELD_CHARS} characters", start)
        raise _BibError("unbalanced braces (no closing brace before the end of the file)", start)

    def quoted(self) -> str:
        assert self.text[self.pos] == '"'
        depth, start = 0, self.pos
        self.pos += 1
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            if ch == "{":
                depth += 1
                if depth > MAX_DEPTH:
                    raise _BibError(f"braces nested deeper than {MAX_DEPTH}", start)
            elif ch == "}":
                depth -= 1
            elif ch == '"' and depth == 0:
                self.pos += 1
                return self.text[start + 1 : self.pos - 1]
            self.pos += 1
            if self.pos - start > MAX_FIELD_CHARS:
                raise _BibError(f"a value longer than {MAX_FIELD_CHARS} characters", start)
        raise _BibError("unterminated quoted value", start)

    def value(self) -> str:
        parts: list[str] = []
        total = 0
        while True:
            self.ws()
            if self.pos >= len(self.text):
                raise _BibError("value missing at the end of the file", self.pos)
            ch = self.text[self.pos]
            if ch == "{":
                parts.append(self.braced())
            elif ch == '"':
                parts.append(self.quoted())
            elif ch.isdigit():
                start = self.pos
                while self.pos < len(self.text) and self.text[self.pos].isdigit():
                    self.pos += 1
                parts.append(self.text[start : self.pos])
            else:
                start = self.pos
                name = self.ident().lower()
                if not name:
                    raise _BibError('a value must be {...}, "...", a number or a macro', start)
                if name not in self.strings:
                    raise _BibError(f"unknown macro {name[:40]!r}", start)
                parts.append(self.strings[name])
            # Checked as the value grows, never after joining (review F6).
            total += len(parts[-1])
            self.expanded += len(parts[-1])
            if total > MAX_FIELD_CHARS:
                raise _BibError(f"a value longer than {MAX_FIELD_CHARS} characters", self.pos)
            if self.expanded > MAX_EXPANSION:
                raise FileRefused("too_large", f"macro expansion beyond {MAX_EXPANSION} characters")
            self.ws()
            if self.pos < len(self.text) and self.text[self.pos] == "#":
                self.pos += 1
                continue
            return "".join(parts)

    def entry(self, at: int) -> tuple[str, str, dict[str, Field], int] | None:
        """Parse one ``@type{key, field = value, ...}`` starting at ``at`` (the ``@``)."""
        self.pos = at + 1
        etype = self.ident().lower()
        self.ws()
        if self.pos >= len(self.text) or self.text[self.pos] not in "{(":
            raise _BibError("an entry type must be followed by { or (", at)
        close = "}" if self.text[self.pos] == "{" else ")"
        self.pos += 1
        if etype == "comment" or etype == "preamble":
            self.pos = at + 1 + len(etype)
            self.ws()
            if self.text[self.pos] == "{":
                self.braced()
            else:
                end = self.text.find(")", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            return None
        if etype == "string":
            self.ws()
            name = self.ident().lower()
            self.ws()
            if not name or self.pos >= len(self.text) or self.text[self.pos] != "=":
                raise _BibError("@string needs name = value", at)
            self.pos += 1
            self.strings[name] = self.value()
            self.ws()
            if self.pos >= len(self.text) or self.text[self.pos] != close:
                raise _BibError("@string is not closed", at)
            self.pos += 1
            return None
        self.ws()
        key_start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ',}) \t\r\n{"=':
            self.pos += 1
        key = self.text[key_start : self.pos]
        self.ws()
        if not key or self.pos >= len(self.text) or self.text[self.pos] != ",":
            raise _BibError("an entry needs a citation key followed by a comma", at)
        self.pos += 1
        fields: dict[str, Field] = {}
        while True:
            self.ws()
            if self.pos >= len(self.text):
                raise _BibError("the entry is not closed before the end of the file", at)
            if self.text[self.pos] == close:
                self.pos += 1
                return etype, key, fields, self.pos
            name_start = self.pos
            name = self.ident().lower()
            self.ws()
            if not name or self.pos >= len(self.text) or self.text[self.pos] != "=":
                raise _BibError("expected field = value", name_start)
            self.pos += 1
            value = self.value()
            if name in fields:
                raise _BibError(f"field {name[:40]!r} appears twice", name_start)
            if len(fields) >= MAX_FIELDS:
                raise _BibError(f"more than {MAX_FIELDS} fields", name_start)
            fields[name] = Field(value, self.line(name_start), self.line(max(self.pos - 1, 0)))
            self.ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1


def _recover(text: str, pos: int) -> int:
    """Where reading goes on after a malformed entry: the next ``@`` that starts a line."""
    match = re.compile(r"(?m)^[ \t]*@").search(text, pos + 1)
    return match.end() - 1 if match else len(text)


def parse_bibtex(text: str) -> Parsed:
    bib = _Bib(text)
    records: list[Record] = []
    keys: set[str] = set()
    pos = 0
    while True:
        at = text.find("@", pos)
        if at < 0:
            break
        try:
            parsed = bib.entry(at)
        except _BibError as exc:
            records.append(
                Record(key=f"line{bib.line(at)}", start_line=bib.line(at),
                       end_line=bib.line(max(exc.pos, at)), error=exc.reason)
            )  # fmt: skip
            pos = _recover(text, at)
            if len(records) > MAX_ENTRIES:
                raise FileRefused("too_many_entries", f"more than {MAX_ENTRIES} entries") from None
            continue
        pos = bib.pos
        if parsed is None:
            continue
        etype, key, fields, end = parsed
        record = Record(key=key, start_line=bib.line(at), end_line=bib.line(end - 1))
        if key in keys:
            record.error = f"citation key {key[:80]!r} appears twice in the file"
        keys.add(key)
        _from_bibtex(record, etype, fields)
        records.append(record)
        if len(records) > MAX_ENTRIES:
            raise FileRefused("too_many_entries", f"more than {MAX_ENTRIES} entries")
    return Parsed("bibtex", records)


def _set(record: Record, name: str, value: str, start: int, end: int) -> None:
    text = clean(value)
    if not text:
        return
    if name in ("title", "venue") and len(text) > MAX_TITLE:
        record.refused_fields.append(
            {"field": name, "value": text[:120], "reason": f"longer than {MAX_TITLE} characters"}
        )
        return
    record.fields[name] = Field(text, start, end)


def _year(record: Record, raw: Field) -> None:
    match = re.search(r"\b(1[0-9]{3}|2[0-9]{3})\b", raw.value)
    if match is None:
        record.refused_fields.append(
            {"field": "year", "value": raw.value[:80], "reason": "no four-digit year"}
        )
        return
    if not 1000 <= int(match.group(1)) <= 2200:
        record.refused_fields.append(
            {"field": "year", "value": raw.value[:80], "reason": "a year is 1000 to 2200"}
        )
        return
    record.fields["year"] = Field(match.group(1), raw.start_line, raw.end_line)


def _authors(record: Record, raw: Field, role: str, separator: str) -> None:
    names = [" ".join(clean(n).split()) for n in re.split(separator, raw.value) if clean(n)]
    names = [n[:300] for n in names if n][:200]
    if not names:
        return
    record.contributors += [{"name": n, "role": role} for n in names]
    previous = record.fields.get("contributors")
    start = raw.start_line if previous is None else min(previous.start_line, raw.start_line)
    end = raw.end_line if previous is None else max(previous.end_line, raw.end_line)
    record.fields["contributors"] = Field(
        "; ".join(
            c["name"] + (" (editor)" if c["role"] == "editor" else "") for c in record.contributors
        ),
        start,
        end,
    )


def _type(raw: str) -> str:
    name = raw.strip().lower()
    name = _TYPE_ALIASES.get(name, name)
    return name if name in ENTRY_TYPES else "misc"


def _from_bibtex(record: Record, etype: str, fields: dict[str, Field]) -> None:
    record.entry_type = _type(etype)
    record.fields["entry_type"] = Field(record.entry_type, record.start_line, record.start_line)
    for name, raw in fields.items():
        if name == "title":
            _set(record, "title", raw.value, raw.start_line, raw.end_line)
        elif name in ("author", "editor"):
            _authors(record, raw, "author" if name == "author" else "editor", r"\s+and\s+")
        elif name in ("year", "date") and "year" not in record.fields:
            _year(record, raw)
        elif name in ("journal", "journaltitle", "booktitle") and "venue" not in record.fields:
            _set(record, "venue", raw.value, raw.start_line, raw.end_line)
        elif name == "abstract":
            _set(record, "abstract", raw.value, raw.start_line, raw.end_line)
    raw_ids = {
        k: fields[k] for k in ("doi", "eprint", "arxiv", "pmid", "pmcid", "isbn") if k in fields
    }
    prefix = clean(fields["archiveprefix"].value).lower() if "archiveprefix" in fields else ""
    if "eprint" in raw_ids and prefix not in ("", "arxiv"):
        del raw_ids["eprint"]  # an eprint of another archive is not an arXiv identifier
    if "eprint" in raw_ids and "arxiv" not in raw_ids:
        raw_ids["arxiv"] = raw_ids.pop("eprint")
    raw_ids.pop("eprint", None)
    if "prometheos_work" in fields:
        record.work_ref = clean(fields["prometheos_work"].value)
    _identify(record, {k: (clean(v.value), v.start_line, v.end_line) for k, v in raw_ids.items()})


# ----- identifiers of a record -------------------------------------------------------------------


def _identify(record: Record, raw: dict[str, tuple[str, int, int]]) -> None:
    """Normalise the identifiers; decide which identify this record and which name another
    version (decision 0062: a journal entry's eprint names its preprint; an arXiv record's DOI
    names its published version)."""
    found: dict[str, tuple[str, int, int]] = {}
    for scheme, (value, start, end) in raw.items():
        try:
            if scheme == "doi":
                found["doi"] = (ids.doi(value), start, end)
            elif scheme == "arxiv":
                identifier, version = ids.arxiv(value)
                found["arxiv"] = (identifier, start, end)
                record.arxiv_version = version
            elif scheme == "pmid":
                found["pmid"] = (ids.pmid(value), start, end)
            elif scheme == "pmcid":
                found["pmcid"] = (ids.pmcid(value), start, end)
            elif scheme == "isbn":
                found["isbn"] = (ids.isbn(value), start, end)
        except ids.Invalid as exc:
            if scheme == "doi" and "more than one" in exc.reason:
                record.error = "conflicting identifiers (more than one DOI): refused for review"
                return
            record.refused_fields.append(
                {"field": f"identifiers.{scheme}", "value": value[:120], "reason": exc.reason}
            )
    doi = found.get("doi")
    if doi and doi[0].startswith("10.48550/arxiv.") and "arxiv" not in found:
        try:
            found["arxiv"] = (ids.arxiv(doi[0].removeprefix("10.48550/arxiv."))[0], doi[1], doi[2])
        except ids.Invalid:
            pass
    arxiv = found.get("arxiv")
    if doi and arxiv and not doi[0].startswith("10.48550/arxiv."):
        venue = record.fields.get("venue")
        preprint = (
            record.entry_type in ("misc", "unpublished", "online", "preprint")
            or venue is None
            or "arxiv" in venue.value.lower()
        )
        if preprint:
            record.related["version_doi"] = doi[0]
            del found["doi"]
        else:
            record.related["version_arxiv"] = arxiv[0]
            del found["arxiv"]
            record.arxiv_version = None
    for scheme, (value, start, end) in found.items():
        # The arXiv record asserts its version too (``2101.00001v2``): the work's version is
        # then chosen with the identifier, by the stable key (review round 2, R2-F1).
        asserted = value + (record.arxiv_version or "") if scheme == "arxiv" else value
        record.fields[f"identifiers.{scheme}"] = Field(asserted, start, end)
        if scheme in ids.IDENTITY_SCHEMES:
            record.identity[scheme] = value


# ----- RIS ----------------------------------------------------------------------------------------

_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")
_RIS_TYPES = {
    "JOUR": "article", "JFULL": "article", "MGZN": "article", "CONF": "inproceedings",
    "CPAPER": "inproceedings", "BOOK": "book", "CHAP": "incollection", "THES": "phdthesis",
    "RPRT": "techreport", "GEN": "misc", "ELEC": "online", "UNPB": "unpublished",
}  # fmt: skip


def parse_ris(text: str) -> Parsed:
    records: list[Record] = []
    current: list[tuple[str, str, int, int]] | None = None
    start = 0
    keys: set[str] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        match = _RIS_LINE.match(line)
        if match is None:
            if current is not None and line.strip():
                if not current:
                    continue
                tag, value, first, _ = current[-1]
                joined = f"{value} {line.strip()}"
                if len(joined) > MAX_FIELD_CHARS:
                    records.append(Record(f"entry{len(records) + 1}", start, number,
                                          error="a value longer than the field limit"))  # fmt: skip
                    current = None
                    continue
                current[-1] = (tag, joined, first, number)
            continue
        tag, value = match.group(1), match.group(2).strip()
        if tag == "TY":
            if current is not None:
                records.append(Record(f"entry{len(records) + 1}", start, number - 1,
                                      error="an entry is not closed by ER"))  # fmt: skip
            current = [(tag, value, number, number)]
            start = number
            continue
        if current is None:
            continue  # text outside an entry is ignored, as a comment
        if tag == "ER":
            record = _from_ris(current, start, number, len(records) + 1)
            if record.key in keys and record.error is None:
                record.error = f"ID {record.key[:80]!r} appears twice in the file"
            keys.add(record.key)
            records.append(record)
            current = None
            if len(records) > MAX_ENTRIES:
                raise FileRefused("too_many_entries", f"more than {MAX_ENTRIES} entries")
            continue
        if len(current) >= MAX_FIELDS * 2:
            records.append(Record(f"entry{len(records) + 1}", start, number,
                                  error=f"more than {MAX_FIELDS * 2} lines"))  # fmt: skip
            current = None
            continue
        if len(value) > MAX_FIELD_CHARS:
            records.append(Record(f"entry{len(records) + 1}", start, number,
                                  error="a value longer than the field limit"))  # fmt: skip
            current = None
            continue
        current.append((tag, value, number, number))
    if current is not None:
        records.append(Record(f"entry{len(records) + 1}", start, len(text.split("\n")),
                              error="an entry is not closed by ER"))  # fmt: skip
    return Parsed("ris", records)


def _from_ris(lines: list[tuple[str, str, int, int]], start: int, end: int, n: int) -> Record:
    by_tag: dict[str, list[tuple[str, int, int]]] = {}
    for tag, value, first, last in lines:
        by_tag.setdefault(tag, []).append((value, first, last))
    ident = by_tag.get("ID")
    key = clean(ident[0][0]) if ident and clean(ident[0][0]) else f"entry{n}"
    record = Record(key=key, start_line=start, end_line=end)
    ty = by_tag["TY"][0]
    record.entry_type = _RIS_TYPES.get(ty[0].upper(), "misc")
    record.fields["entry_type"] = Field(record.entry_type, ty[1], ty[2])

    def pick(*tags: str) -> tuple[str, int, int] | None:
        for tag in tags:
            if tag in by_tag:
                return by_tag[tag][0]
        return None

    title = pick("TI", "T1")
    if title:
        _set(record, "title", title[0], title[1], title[2])
    for tags, role in ((("AU", "A1"), "author"), (("A2", "ED"), "editor")):
        for tag in tags:
            for value, a, b in by_tag.get(tag, []):
                _authors(record, Field(value, a, b), role, r"(?!)")
    year = pick("PY", "Y1", "DA")
    if year:
        _year(record, Field(year[0], year[1], year[2]))
    venue = pick("JO", "JF", "T2", "JA", "BT")
    if venue:
        _set(record, "venue", venue[0], venue[1], venue[2])
    abstract = pick("AB", "N2")
    if abstract:
        _set(record, "abstract", abstract[0], abstract[1], abstract[2])
    if len(by_tag.get("DO", [])) > 1:
        record.error = "conflicting identifiers (more than one DOI): refused for review"
        return record
    raw: dict[str, tuple[str, int, int]] = {}
    if "DO" in by_tag:
        raw["doi"] = by_tag["DO"][0]
    for value, a, b in by_tag.get("UR", []):
        if "arxiv.org/abs/" in value.lower() and "arxiv" not in raw:
            raw["arxiv"] = (value, a, b)
    accession = pick("AN")
    database = " ".join(v for tag in ("DB", "DP") for v, _a, _b in by_tag.get(tag, [])).lower()
    pubmed = any(word in database for word in ("pubmed", "medline", "nlm"))
    if accession and pubmed and re.fullmatch(r"\s*[0-9]{1,10}\s*", accession[0]):
        raw["pmid"] = accession  # an accession number is a PMID only in a PubMed record
    serial = pick("SN")
    if serial is not None:
        digits = re.sub(r"[\s-]", "", serial[0])
        if digits[:3] in ("978", "979") or len(digits) == 10:  # an ISBN, not an ISSN
            raw["isbn"] = serial
    for value, _a, _b in by_tag.get("C8", []):
        if value.startswith("prometheos:"):
            record.work_ref = value.removeprefix("prometheos:").strip()
    _identify(record, {k: (clean(v[0]), v[1], v[2]) for k, v in raw.items()})
    return record


# ----- CSV ----------------------------------------------------------------------------------------

STANDARD_COLUMNS = {
    "title": "title", "author": "contributors", "authors": "contributors",
    "year": "year", "journal": "venue", "venue": "venue", "booktitle": "venue",
    "publication": "venue", "doi": "doi", "arxiv": "arxiv", "eprint": "arxiv", "pmid": "pmid",
    "pmcid": "pmcid", "isbn": "isbn", "key": "key", "id": "key", "citekey": "key",
    "type": "entry_type", "prometheos_work": "work_ref", "annotations": "ignore",
    "abstract": "abstract",
}  # fmt: skip
CSV_TARGETS = frozenset(STANDARD_COLUMNS.values())
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def needs_escape(value: str) -> bool:
    """A cell a spreadsheet could run as a formula, or one that looks like an escaped one."""
    if value[:1] in FORMULA_STARTS:
        return True
    return value[:1] == "'" and needs_escape(value[1:])


def unescape_cell(value: str) -> str:
    """Undo the export's formula-injection escape: exactly one leading ``'`` that the export
    added (it adds one before ``= + - @``, a tab, a CR, or a value that already looks escaped)."""
    return value[1:] if value[:1] == "'" and needs_escape(value[1:]) else value


def parse_csv(text: str, mapping: dict[str, str] | None = None) -> Parsed:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return Parsed("csv", [])
    except csv.Error as exc:
        raise FileRefused("malformed", f"the CSV header cannot be read: {exc}") from exc
    columns: dict[int, str] = {}
    unmapped: list[str] = []
    declared = {k.strip().lower(): v for k, v in (mapping or {}).items()}
    for index, name in enumerate(header):
        label = name.strip().lower()
        target = declared.get(label) or STANDARD_COLUMNS.get(label)
        if target is None or target not in CSV_TARGETS:
            unmapped.append(name.strip()[:80])
        elif target != "ignore":
            columns[index] = target
    records: list[Record] = []
    previous_end = reader.line_num
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            records.append(Record(f"row{len(records) + 1}", previous_end + 1, previous_end + 1,
                                  error=f"malformed CSV row: {exc}"[:200]))  # fmt: skip
            break
        start, end = previous_end + 1, reader.line_num
        previous_end = end
        if not any(cell.strip() for cell in row):
            continue
        if len(records) >= MAX_ENTRIES:
            raise FileRefused("too_many_entries", f"more than {MAX_ENTRIES} rows")
        values = {columns[i]: unescape_cell(cell) for i, cell in enumerate(row) if i in columns}
        if any(len(v) > MAX_FIELD_CHARS for v in values.values()):
            records.append(Record(f"row{len(records) + 1}", start, end,
                                  error="a value longer than the field limit"))  # fmt: skip
            continue
        key = clean(values.get("key", "")) or f"row{len(records) + 1}"
        record = Record(key=key, start_line=start, end_line=end)
        etype = _type(values.get("entry_type", "") or "misc")
        record.entry_type = etype
        record.fields["entry_type"] = Field(etype, start, end)
        if values.get("title"):
            _set(record, "title", values["title"], start, end)
        if values.get("contributors"):
            _authors(record, Field(values["contributors"], start, end), "author", r"\s*;\s*")
        if values.get("year"):
            _year(record, Field(values["year"], start, end))
        if values.get("venue"):
            _set(record, "venue", values["venue"], start, end)
        if values.get("abstract"):
            _set(record, "abstract", values["abstract"], start, end)
        if values.get("work_ref"):
            record.work_ref = values["work_ref"].strip()
        raw = {
            s: (clean(values[s]), start, end)
            for s in ("doi", "arxiv", "pmid", "pmcid", "isbn")
            if clean(values.get(s, ""))
        }
        _identify(record, raw)
        records.append(record)
    return Parsed("csv", records, unmapped_columns=unmapped,
                  column_map={header[i].strip()[:80]: t for i, t in columns.items()})  # fmt: skip


def parse(fmt: str, text: str, mapping: dict[str, str] | None = None) -> Parsed:
    if fmt == "bibtex":
        return parse_bibtex(text)
    if fmt == "ris":
        return parse_ris(text)
    return parse_csv(text, mapping)


def values_of(record: Record) -> dict[str, Any]:
    """The work field values a record asserts (strings, as provenance stores them)."""
    return {name: f.value for name, f in record.fields.items() if name != "abstract"}
