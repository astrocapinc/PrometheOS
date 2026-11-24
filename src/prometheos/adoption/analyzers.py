"""The deterministic analyzer pack (A05, CP17.2; decision 0040; C17-LINEAGE).

Two layers, both pure and replayable:

- **Extractors** read one inventory row's bytes (read-only, re-hashed against the inventory)
  and return a JSON-able summary. Their output is cached by
  ``(analyzer, version, params digest, input sha256)``, so a re-adoption re-runs only what
  changed (C17-INCREMENTAL).
- **Linkers** run over the whole inventory and the extractor outputs and emit typed evidence
  rows with a tier: they resolve names (functions, models, files mentioned in code or text)
  against inventory paths, group identical content, read naming conventions, and turn status
  lines in notes into located statements with verbatim quotes.

Tiers follow R4 3.3 and decision 0039: T1 app-observed identity and embedded metadata, T2 a
producing tool's own convention (MATLAB's ``.asv`` autosave, Simulink's release-upgrade backup),
T3 declared structure (calls, model references, project membership), T4 explicit human
statements with a verbatim quote, T6 static literal references, T7 naming heuristics and
near-duplicate similarity. Nothing here is T0 (the owner) or T8 (AI). Analyzer declarations
are the exported ``adoption_analyzer`` contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from prometheos.adoption import matlab, profiles
from prometheos.adoption.contracts import (
    MEMBER_SEPARATOR,
    AdoptionAnalyzer,
    AdoptionEvidence,
    AdoptionFile,
    AdoptionProfile,
    disk_path,
    escape_segment,
)
from prometheos.adoption.inventory import PLACEHOLDER_BITS, decode_text, kind_of, os_path
from prometheos.domain.canonical import canonical_sha256

MAX_READ = 64 * 1024 * 1024
MAT_PREFIX = 4 * 1024 * 1024
MAX_STATEMENT_LINE = 600


def _declare(**values: Any) -> AdoptionAnalyzer:
    defaults = {"kind": "builtin", "determinism": "pure"}
    return AdoptionAnalyzer.model_validate({**defaults, **values})


ANALYZERS: dict[str, AdoptionAnalyzer] = {
    a.id: a
    for a in (
        _declare(
            id="identity.copies",
            version="1.0.0",
            summary="Byte-identical and normalised-text copies (equal digests)",
            selects={"containers": []},
            reads="inventory_only",
            max_bytes=0,
            emits=["copy_of"],
            tiers=["T1"],
        ),
        _declare(
            id="identity.near_copies",
            version="1.1.0",
            summary="Near-duplicate text among same-stem candidates (MinHash over shingles)",
            selects={"containers": ["text"]},
            reads="whole_file",
            max_bytes=2 * 1024 * 1024,
            emits=["copy_of"],
            tiers=["T7"],
        ),
        _declare(
            id="name.conventions",
            version="1.0.0",
            summary="Version tokens in names; MATLAB autosaves and Simulink upgrade backups",
            selects={"containers": []},
            reads="inventory_only",
            max_bytes=0,
            emits=["facet", "autosave_of", "backup_of"],
            tiers=["T2", "T7"],
        ),
        _declare(
            id="profile.status_terms",
            kind="declarative",
            version="1.0.0",
            summary="The scan profile's own status words in notes (read like the builtin ones)",
            selects={"containers": ["text"], "extensions": [".md", ".txt", ".markdown"]},
            reads="whole_file",
            max_bytes=MAX_READ,
            emits=["has_status", "unresolved"],
            tiers=["T4", "T8"],
        ),
        _declare(
            id="profile.version_tokens",
            kind="declarative",
            version="1.0.0",
            summary="The scan profile's version tokens stripped from names (family stems)",
            selects={"containers": []},
            reads="inventory_only",
            max_bytes=0,
            emits=["facet"],
            tiers=["T7"],
        ),
        _declare(
            id="code.matlab",
            version="1.0.0",
            summary="MATLAB or Objective-C; definitions, calls, file and model literals",
            selects={"containers": ["text", "opc_mlx"], "extensions": [".m", ".asv", ".mlx"]},
            reads="whole_file",
            max_bytes=MAX_READ,
            emits=["language", "facet", "calls", "used", "generated_by", "model_reference"],
            tiers=["T1", "T3", "T6"],
        ),
        _declare(
            id="opc.simulink",
            version="1.0.0",
            summary="Simulink and live-script packages: identity, release, revision, references",
            selects={
                "containers": ["opc_slx", "opc_mlx", "opc_slxc", "text"],
                "extensions": [".slx", ".mdl", ".mlx", ".slxc"],
            },
            reads="container_parts",
            max_bytes=MAX_READ,
            emits=["facet", "same_origin_as", "model_reference", "library_link", "cache_of"],
            tiers=["T1", "T3"],
        ),
        _declare(
            id="data.mat",
            version="1.0.0",
            summary="MAT-file version, platform, creation time and variable directory",
            selects={"containers": ["mat_v4", "mat_v5", "mat_v73"]},
            reads="prefix",
            max_bytes=MAT_PREFIX,
            emits=["facet"],
            tiers=["T1"],
        ),
        _declare(
            id="matlab.project",
            version="1.0.0",
            summary="MATLAB project definition: member files and their labels",
            selects={"extensions": [".prj", ".toml"]},
            reads="directory",
            max_bytes=MAX_READ,
            emits=["project_file", "unresolved"],
            tiers=["T3"],
        ),
        _declare(
            id="text.statements",
            version="1.0.0",
            summary="Status words in notes that name inventory files, with verbatim quotes",
            selects={"containers": ["text"], "extensions": [".md", ".txt", ".markdown"]},
            reads="whole_file",
            max_bytes=2 * 1024 * 1024,
            emits=["has_status", "supersedes", "facet"],
            tiers=["T1", "T4"],
        ),
        _declare(
            id="text.references",
            version="1.0.0",
            summary="Inventory paths mentioned in notes and code",
            selects={"containers": ["text"]},
            reads="whole_file",
            max_bytes=2 * 1024 * 1024,
            emits=["references"],
            tiers=["T6"],
        ),
        _declare(
            id="graphify.import",
            version="1.0.0",
            summary="An existing Graphify graph, read only, with a freshness check per file",
            selects={"extensions": [".json"]},
            reads="whole_file",
            max_bytes=MAX_READ,
            emits=["calls", "references", "facet"],
            tiers=["T1", "T3", "T8"],
        ),
        # CP17.4 (decision 0042): results the coding agent reports, accepted by verification
        _declare(
            id="ai.status_statements",
            version="1.0.0",
            kind="ai_task",
            determinism="proposed",
            summary="Status statements an AI read, each with a quote re-read from the bytes",
            selects={"containers": ["text"]},
            reads="whole_file",
            max_bytes=2 * 1024 * 1024,
            emits=["has_status", "unresolved"],
            tiers=["T4", "T8"],
        ),
        _declare(
            id="ai.classify_file",
            version="1.0.0",
            kind="ai_task",
            determinism="proposed",
            summary="A file's role as an AI reads it (a hint, never decisive)",
            selects={"containers": ["text"]},
            reads="prefix",
            max_bytes=4096,
            emits=["facet"],
            tiers=["T8"],
        ),
        # A06.1 under A07 (decision 0107): outline items an AI or the agent read, each a
        # verified quote; class proposals are kept beside the family, never as evidence
        _declare(
            id="ai.outline",
            version="1.0.0",
            kind="ai_task",
            determinism="proposed",
            summary="Questions, deliverables and tracks a note names, each a verified quote",
            selects={"containers": ["text"]},
            reads="whole_file",
            max_bytes=2 * 1024 * 1024,
            emits=["facet"],
            tiers=["T4"],
        ),
        _declare(
            id="ai.family_class",
            version="1.0.0",
            kind="ai_task",
            determinism="proposed",
            summary="What a competing family's files are, as a proposal the owner answers",
            selects={"containers": ["text"]},
            reads="prefix",
            max_bytes=4096,
            emits=["facet"],
            tiers=["T8"],
        ),
        _declare(
            id="agent.matlab_dependencies",
            version="1.0.0",
            kind="agent_task",
            determinism="reported",
            summary="MATLAB's own dependency analysis, run by the coding agent and re-checked",
            selects={"extensions": [".m"]},
            reads="inventory_only",
            max_bytes=0,
            emits=["used", "unresolved"],
            tiers=["T3"],
        ),
    )
}


# ----- facts ---------------------------------------------------------------------------------


@dataclass
class Fact:
    analyzer: str
    subject: str
    predicate: str
    object: dict[str, Any]
    tier: str
    locator: dict[str, Any] | None = None
    quote: str | None = None

    def evidence(self, scan_gen: int, subject_sha256: str | None) -> AdoptionEvidence:
        declaration = ANALYZERS[self.analyzer]
        quote_sha = hashlib.sha256(self.quote.encode("utf-8")).hexdigest() if self.quote else None
        key = {
            "scan_gen": scan_gen,
            "analyzer": self.analyzer,
            "version": declaration.version,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "locator": self.locator,
            "quote": quote_sha,
        }
        return AdoptionEvidence.model_validate(
            {
                "evidence_id": "ev_" + canonical_sha256(key)[:32],
                "scan_gen": scan_gen,
                "analyzer": self.analyzer,
                "analyzer_version": declaration.version,
                "subject": self.subject,
                "subject_sha256": subject_sha256,
                "predicate": self.predicate,
                "object": self.object,
                "locator": self.locator,
                "quote": self.quote,
                "quote_sha256": quote_sha,
                "tier": self.tier,
                "determinism": declaration.determinism,
            }
        )


# ----- the corpus seen by linkers ------------------------------------------------------------

VERSION_TOKENS: tuple[str, ...] = (
    r"[_\- ]?v\d+[a-z]?$",
    r"[_\- ]?rev\d*[a-z]?$",
    r"[_\- ]?final\d*$",
    r"[_\- ]?best$",
    r"[_\- ]?latest$",
    r"[_\- ]?old$",
    r"[_\- ]?new$",
    r"[_\- ]?copy( \d+)?$",
    r" - copy( \(\d+\))?$",
    r"[_\- ]?\(\d+\)$",
    r"[_\- ]?backup$",
    r"[_\- ]?bak$",
    r"[_\- ]?draft$",
    r"[_\- ]?(19|20)\d{2}([_\-.]?\d{1,2}){0,2}$",
    r"[_\- ]?\d{1,2}[_\-.]\d{1,2}[_\-.]\d{2,4}$",
    r"[_\- ]?\d{8}$",
)
_TOKENS = [re.compile(t, re.IGNORECASE) for t in VERSION_TOKENS]
_TOOL_SUFFIX = re.compile(r"\.(slx|mdl|sldd)\.r\d{4}[ab]$", re.IGNORECASE)


def name_stem(path: str) -> tuple[str, list[str]]:
    """The family stem of a file name and the version tokens stripped from it: lower case,
    extensions and tool suffixes removed, then trailing tokens (``_v3``, ``FINAL``, ``backup``,
    dates, ``(1)``...) removed repeatedly."""
    name = PurePosixPath(path.split(MEMBER_SEPARATOR)[-1]).name.lower()
    name = _TOOL_SUFFIX.sub("", name)
    while "." in name.strip("."):
        name = name.rsplit(".", 1)[0]
    tokens: list[str] = []
    changed = True
    while changed and name:
        changed = False
        for pattern in _TOKENS:
            found = pattern.search(name)
            if found and found.start() > 0:
                tokens.append(found.group(0).strip("_- ").lower())
                name = name[: found.start()]
                changed = True
    return name.strip("_- ."), tokens


@dataclass
class Corpus:
    rows: dict[str, AdoptionFile]
    outputs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (analyzer, path)
    profile: AdoptionProfile | None = None

    def __post_init__(self) -> None:
        terms = self.profile.vocabulary.status_terms if self.profile is not None else {}
        self.profile_terms = {t for t in terms if t not in STATUS_WORDS}
        self.status_words = {**{t: terms[t] for t in self.profile_terms}, **STATUS_WORDS}
        self.status_regex = re.compile(
            r"\b(?:"
            + "|".join(re.escape(w) for w in sorted(self.status_words, key=len, reverse=True))
            + r")\b",
            re.I,
        )
        self.version_patterns = profiles.token_patterns(self.profile)
        self.by_stem: dict[str, list[str]] = defaultdict(list)
        self.by_lower: dict[str, str] = {}
        for path, row in sorted(self.rows.items()):
            if row.entry not in ("file", "member"):
                continue
            self.by_lower.setdefault(path.lower(), path)
            stem = PurePosixPath(path.split(MEMBER_SEPARATOR)[-1]).stem.lower()
            self.by_stem[stem].append(path)

    def files(self) -> Iterable[AdoptionFile]:
        return (r for r in self.rows.values() if r.entry == "file")

    def resolve_path(self, literal: str, base: str) -> str | None:
        """An inventory path for a literal path in a file at ``base``: relative to the file's
        folder first, then to the root; extensions ``.mat``/``.slx``/``.mdl`` tried when the
        literal has none; case-insensitive (Windows)."""
        text = literal.replace("\\", "/").strip()
        if (
            not text
            or len(text) > 400
            or text.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:", text)
        ):
            return None
        folder = PurePosixPath(base).parent.as_posix()
        candidates = [f"{folder}/{text}" if folder not in ("", ".") else text, text]
        out: list[str] = []
        for candidate in candidates:
            parts: list[str] = []
            for part in candidate.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(part)
            out.append("/".join(parts))
        for candidate in out:
            candidate = "/".join(escape_segment(p) for p in candidate.split("/"))
            for suffix in ("", ".mat", ".slx", ".mdl", ".m"):
                hit = self.by_lower.get((candidate + suffix).lower())
                if hit is not None:
                    return hit
        return None

    def functions(self, name: str) -> list[str]:
        """Inventory ``.m`` files (not autosaves) whose stem is ``name``."""
        return [p for p in self.by_stem.get(name.lower(), []) if p.lower().endswith(".m")]

    def models(self, name: str) -> list[str]:
        """Model files (``.slx``/``.mdl``) MATLAB would load for ``name``: those whose file stem
        is the name; when none is, those whose embedded model name is (an upgrade backup such
        as ``x.slx.r2024b`` is never a match: MATLAB does not load it by name)."""
        stem = PurePosixPath(name.replace("\\", "/")).name
        stem = stem.rsplit(".", 1)[0] if stem.lower().endswith((".slx", ".mdl")) else stem
        models = (".slx", ".mdl")
        hits = {p for p in self.by_stem.get(stem.lower(), []) if p.lower().endswith(models)}
        if hits:
            return sorted(hits)
        for (analyzer, path), out in self.outputs.items():
            if analyzer == "opc.simulink" and (out.get("model_name") or "").lower() == stem.lower():
                if path.lower().endswith(models):
                    hits.add(path)
        return sorted(hits)


# ----- extractors (per file, cached) ---------------------------------------------------------


def _code_matlab(path: str, data: bytes, row: AdoptionFile) -> dict[str, Any]:
    source = data
    if row.container == "opc_mlx":
        source = (matlab.read_package(data).code or "").encode("utf-8")
    s = matlab.parse_matlab(source)

    def sites(items: list[matlab.Site]) -> list[dict[str, Any]]:
        return [
            {"name": i.name[:300], "line": i.line, "quote": i.text[:MAX_STATEMENT_LINE]}
            for i in items
        ]

    return {
        "language": s.language,
        "parsed": s.parsed,
        "errors": s.errors,
        "definitions": sites(s.definitions),
        "calls": [c for c in sites(s.calls) if c["name"] not in s.assigned],
        "reads": sites(s.reads),
        "writes": sites(s.writes),
        "models": sites(s.models),
    }


def _opc_simulink(path: str, data: bytes, row: AdoptionFile) -> dict[str, Any]:
    if path.lower().endswith(".mdl") or row.container == "text":
        package = matlab.read_mdl(data.decode("utf-8", "replace"))
    else:
        package = matlab.read_package(data)
    return {
        "content_type": package.content_type,
        "release": package.release,
        "uuid": package.uuid,
        "revision": package.revision,
        "created": package.created,
        "modified": package.modified,
        "version": package.version,
        "model_name": package.model_name,
        "model_references": package.model_references[:500],
        "library_links": sorted(set(package.library_links))[:500],
        "variables_read": package.variables_read[:200],
        "variables_written": package.variables_written[:200],
        "problems": package.problems[:20],
    }


def _data_mat(path: str, data: bytes, row: AdoptionFile) -> dict[str, Any]:
    header = matlab.read_mat_header(data)
    return {
        "version": header.version,
        "platform": header.platform,
        "created": header.created,
        "variables": header.variables[:200],
        "problems": header.problems,
        "truncated": len(data) >= MAT_PREFIX,
    }


_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def _text_lines(path: str, data: bytes, row: AdoptionFile) -> dict[str, Any]:
    """The lines of a text file that mention an inventory-looking path, with frontmatter.
    Only what a reader sees counts, and code blocks are not statements."""
    text = decode_text(data) or ""
    front: dict[str, str] = {}
    found = _FRONTMATTER.match(text)
    if found:
        for line in found.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                if re.fullmatch(r"[A-Za-z_][\w-]{0,40}", key.strip()):
                    front[key.strip().lower()] = value.strip()[:200]
    lines = []
    hidden = instructions = 0
    raw = text.splitlines()
    for seen in visible_text(text):
        if seen.hidden:
            hidden += 1
            instructions += 1 if _INSTRUCTION.search(_reveal(raw[seen.number - 1])) else 0
        if seen.code:
            continue
        if len(seen.text) <= MAX_STATEMENT_LINE and re.search(
            r"[\w.\-]+/[\w.\-/]+|\w+\.\w{1,6}\b", seen.text
        ):
            lines.append({"line": seen.number, "text": seen.text})
        if len(lines) >= 2000:
            break
    return {
        "frontmatter": front,
        "lines": lines,
        "hidden_lines": hidden,
        "hidden_instructions": instructions,
    }


_INSTRUCTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|\b(ai|llm) (assistant|agent)s?\b|"
    r"\bsystem prompt\b|\bdelete\b|\bmark .{0,60} as (current|final)",
    re.IGNORECASE,
)


# ----- what a reader sees (review cp17-234-a F7, F8) ------------------------------------------

_INVISIBLE_CHARS = re.compile("[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮⁠-⁯ㅤ︀-️﻿ﾠ￰-￻\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]*\]:\s*\S")
# Bounded (review cp17-234-a r2 R2-2): a line of "<a<a<a..." without ">" was quadratic.
_OPEN_TAG = re.compile(r"<([A-Za-z][\w-]{0,40})\b([^<>\n]{0,2000})>")
_HIDDEN_TAGS = frozenset({"style", "script", "template", "noscript"})
_HIDDEN_ATTRIBUTE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|(?<![\w-])hidden(?![\w-])"
    r"|aria-hidden\s*=\s*[\"']?true",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Seen:
    number: int  # 1-based line number in the file
    text: str  # what a reader sees on the line
    hidden: bool  # part of the line was not visible (comment, hidden element, invisible chars)
    code: bool  # inside a fenced code block


def strip_invisible(text: str) -> tuple[str, int]:
    """Text without characters a reader does not see: format and control characters (tab
    kept), Unicode tag characters (an instruction can be spelt with them), variation selectors,
    soft hyphens and fillers. Returns the text and how many were removed."""
    out: list[str] = []
    removed = 0
    for ch in text:
        if ch != "\t" and (
            unicodedata.category(ch) in ("Cf", "Cc") or _INVISIBLE_CHARS.match(ch) is not None
        ):
            removed += 1
            continue
        out.append(ch)
    return "".join(out), removed


def _reveal(line: str) -> str:
    """The line with Unicode tag characters read as the ASCII they encode (for flagging)."""
    return "".join(chr(ord(ch) - 0xE0000) if 0xE0020 <= ord(ch) <= 0xE007E else ch for ch in line)


def visible_text(text: str) -> list[Seen]:
    """Every line of a Markdown or text file as a reader of the rendered note sees it. Removed:
    HTML comments (also across lines), link reference definitions (``[//]: # (...)`` and any
    other: they never render), elements that are hidden (``display:none``, ``hidden``,
    ``aria-hidden``) or never shown (style, script, template), and invisible characters.
    ``<!--`` and tags inside inline code or a fenced block are text, not markup. One function
    serves the analyzers and the agent's bundle, so both see the same text."""
    out: list[Seen] = []
    in_comment = in_fence = False
    fence = ""
    hidden_tag: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if in_fence:
            if line.strip().startswith(fence):
                in_fence = False
            shown, removed = strip_invisible(line)
            out.append(Seen(number, shown, removed > 0, True))
            continue
        if not in_comment and hidden_tag is None:
            opening = _FENCE.match(line)
            if opening:
                in_fence, fence = True, opening.group(1)
                shown, removed = strip_invisible(line)
                out.append(Seen(number, shown, removed > 0, True))
                continue
            if _LINK_DEFINITION.match(line):
                out.append(Seen(number, "", True, False))
                continue
        parts: list[str] = []
        hidden = in_comment or hidden_tag is not None
        position = 0
        last_close = line.rfind(">")
        while position < len(line):
            if in_comment:
                end = line.find("-->", position)
                if end == -1:
                    position = len(line)
                    break
                in_comment = False
                position = end + 3
                continue
            if hidden_tag is not None:
                close = re.compile(rf"</\s*{re.escape(hidden_tag)}\s*>", re.IGNORECASE)
                end_match = close.search(line, position)
                if end_match is None:
                    position = len(line)
                    break
                hidden_tag = None
                position = end_match.end()
                continue
            ch = line[position]
            if ch == "`":
                run = len(line) - len(line[position:].lstrip("`"))
                ticks = line[position : position + run]
                end = line.find(ticks, position + run)
                if end == -1:
                    parts.append(ticks)
                    position += run
                else:
                    parts.append(line[position : end + run])
                    position = end + run
                continue
            if line.startswith("<!--", position):
                hidden, in_comment = True, True
                position += 4
                continue
            if ch == "<" and position < last_close:
                tag = _OPEN_TAG.match(line, position)
                if tag is not None and (
                    tag.group(1).lower() in _HIDDEN_TAGS or _HIDDEN_ATTRIBUTE.search(tag.group(2))
                ):
                    hidden, hidden_tag = True, tag.group(1).lower()
                    position = tag.end()
                    continue
            parts.append(ch)
            position += 1
        shown, removed = strip_invisible("".join(parts))
        out.append(Seen(number, shown, hidden or removed > 0, False))
    return out


EXTRACTORS: dict[str, Callable[[str, bytes, AdoptionFile], dict[str, Any]]] = {
    "code.matlab": _code_matlab,
    "opc.simulink": _opc_simulink,
    "data.mat": _data_mat,
    "text.statements": _text_lines,
}


def unavailable() -> dict[str, str]:
    """Extractors that cannot run in this installation, with why (decision 0053). They are not
    run, their cached outputs are not replayed, and the scan's analysis summary names them."""
    problem = matlab.parser_problem()
    return {"code.matlab": problem} if problem is not None else {}


INSTALL_HINT = {"code.matlab": f"install prometheos[{matlab.PARSER_EXTRA}] and scan again"}


def applies(analyzer: str, row: AdoptionFile) -> bool:
    if row.entry != "file" or row.status not in ("read", "limited") or row.sha256 is None:
        return False
    name = row.path.lower()
    if analyzer == "code.matlab":
        return (
            name.endswith((".m", ".asv")) and row.container == "text"
        ) or row.container == "opc_mlx"
    if analyzer == "opc.simulink":
        return row.container in ("opc_slx", "opc_slxc") or (
            name.endswith(".mdl") and row.container == "text"
        )
    if analyzer == "data.mat":
        return row.container in ("mat_v4", "mat_v5", "mat_v73")
    if analyzer == "text.statements":
        return row.container == "text" and name.endswith((".md", ".txt", ".markdown"))
    return False


def params_digest(analyzer: str) -> str:
    return canonical_sha256({"analyzer": analyzer, "params": {}})


# ----- linkers (whole corpus) ----------------------------------------------------------------

STATUS_WORDS: dict[str, str] = {
    "superseded": "superseded",
    "obsolete": "superseded",
    "replaced by": "superseded",
    "deprecated": "superseded",
    "invalid": "invalidated",
    "invalidated": "invalidated",
    "withdrawn": "withdrawn",
    "do not use": "invalidated",
    "wrong": "invalidated",
    "is the current": "current",
    "current result": "current",
    "of record": "current",
    "current version": "current",
    "is current": "current",
    "final result": "final_claimed",
    "is final": "final_claimed",
    "is the final": "final_claimed",
    "draft": "draft",
    "archived": "archived",
    "failed": "failed",
}
_PATHLIKE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)*\.\w{1,6}|[\w.\-]+(?:/[\w.\-]+)+")
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def link(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    facts += _copies(corpus)
    facts += _conventions(corpus)
    facts += _profile_stems(corpus)
    facts += _code_links(corpus)
    facts += _package_links(corpus)
    facts += _mat_facets(corpus)
    facts += _statements(corpus)
    return facts


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _masked(name: str, quote: str) -> tuple[str, str]:
    """An absolute path written in the researcher's code names someone's machine (a user
    folder): it is kept as `<absolute>/` plus its file name and a digest of the whole path,
    in the name and in the quoted line (review cp17-234-a F23)."""
    if not _ABSOLUTE.match(name):
        return name, quote
    base = re.split(r"[\\/]", name.rstrip("\\/"))[-1]
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    shown = f"<absolute:{digest}>/{base}"
    return shown, quote.replace(name, shown)


def _copies(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    by_sha: dict[str, list[str]] = defaultdict(list)
    by_text: dict[str, list[str]] = defaultdict(list)
    for row in corpus.files():
        if row.sha256 and row.status == "read":
            by_sha[row.sha256].append(row.path)
            if row.text_sha256:
                by_text[row.text_sha256].append(row.path)
    for members in _meaningful(corpus, by_sha.values()):
        if len(members) > 1:
            first, *rest = sorted(members)
            for other in rest:
                facts.append(
                    Fact(
                        "identity.copies",
                        other,
                        "copy_of",
                        {"path": first, "literal": {"level": "exact"}},
                        "T1",
                    )
                )
    for members in _meaningful(corpus, by_text.values()):
        shas = {corpus.rows[m].sha256 for m in members}
        if len(shas) > 1:
            first, *rest = sorted(members)
            for other in rest:
                if corpus.rows[other].sha256 != corpus.rows[first].sha256:
                    facts.append(
                        Fact(
                            "identity.copies",
                            other,
                            "copy_of",
                            {"path": first, "literal": {"level": "normalised"}},
                            "T1",
                        )
                    )
    return facts


TRIVIAL_BYTES = 64


def _meaningful(corpus: Corpus, groups: Iterable[list[str]]) -> list[list[str]]:
    """Groups of equal content, with trivial files (empty, a `TODO` line: under 64 bytes)
    kept together only when their names agree: two empty `__init__.py` files are copies, an
    empty table and an empty note are not (review cp17-234-a F18)."""
    out: list[list[str]] = []
    for members in groups:
        size = corpus.rows[members[0]].size
        if size is not None and size < TRIVIAL_BYTES:
            by_name: dict[str, list[str]] = defaultdict(list)
            for path in members:
                by_name[name_stem(path)[0] or path.rsplit("/", 1)[-1].lower()].append(path)
            out.extend(by_name.values())
        else:
            out.append(members)
    return out


def profile_stem(path: str, patterns: list[re.Pattern[str]]) -> tuple[str, list[str]]:
    """The builtin stem with the profile's version tokens also stripped (repeatedly)."""
    stem, tokens = name_stem(path)
    changed = True
    while changed and patterns:
        changed = False
        for pattern in patterns:
            found = pattern.search(stem)
            if found and found.start() > 0:
                tokens = [found.group(0).strip("_- "), *tokens]
                stem = stem[: found.start()]
                changed = True
    return stem, tokens


def _profile_stems(corpus: Corpus) -> list[Fact]:
    """`profile.stem` facets where the profile's tokens give a file another stem (T7)."""
    facts: list[Fact] = []
    if not corpus.version_patterns:
        return facts
    for row in corpus.files():
        stem, tokens = profile_stem(row.path, corpus.version_patterns)
        if stem != name_stem(row.path)[0]:
            facts.append(
                Fact(
                    "profile.version_tokens",
                    row.path,
                    "facet",
                    {"facet": "profile.stem", "literal": {"stem": stem, "tokens": tokens}},
                    "T7",
                )
            )
    return facts


def _conventions(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    for row in corpus.files():
        stem, tokens = name_stem(row.path)
        facts.append(
            Fact(
                "name.conventions",
                row.path,
                "facet",
                {"facet": "name.stem", "literal": {"stem": stem, "tokens": tokens}},
                "T7",
            )
        )
        lower = row.path.lower()
        if lower.endswith(".asv"):
            twin = corpus.by_lower.get(lower[:-4] + ".m")
            if twin is not None:
                facts.append(
                    Fact("name.conventions", row.path, "autosave_of", {"path": twin}, "T2")
                )
        tool = _TOOL_SUFFIX.search(lower)
        if tool:
            twin = corpus.by_lower.get(lower[: tool.start()] + "." + tool.group(1))
            if twin is not None:
                facts.append(
                    Fact(
                        "name.conventions",
                        row.path,
                        "backup_of",
                        {
                            "path": twin,
                            "literal": {"release": lower[tool.start() + len(tool.group(1)) + 2 :]},
                        },
                        "T2",
                    )
                )
    return facts


def _line_locator(line: int) -> dict[str, Any]:
    return {"kind": "line", "start": line, "end": line}


def _code_links(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    for (analyzer, path), out in sorted(corpus.outputs.items()):
        if analyzer != "code.matlab":
            continue
        facts.append(Fact("code.matlab", path, "language", {"literal": out["language"]}, "T1"))
        if out["language"] != "matlab":
            continue
        if out["errors"]:
            facts.append(
                Fact(
                    "code.matlab",
                    path,
                    "facet",
                    {"facet": "code.parse_errors", "literal": out["errors"]},
                    "T1",
                )
            )
        own = {d["name"] for d in out["definitions"]}
        seen: set[tuple[str, str]] = set()
        for call in out["calls"]:
            if call["name"] in own:
                continue
            for target in corpus.functions(call["name"]):
                if target == path or ("calls", target) in seen:
                    continue
                seen.add(("calls", target))
                facts.append(
                    Fact(
                        "code.matlab",
                        path,
                        "calls",
                        {"path": target},
                        "T3",
                        _line_locator(call["line"]),
                        call["quote"],
                    )
                )
        for kind, predicate in (
            ("reads", "used"),
            ("writes", "generated_by"),
            ("models", "model_reference"),
        ):
            for site in out[kind]:
                if predicate == "model_reference":
                    targets = corpus.models(site["name"])
                else:
                    resolved = corpus.resolve_path(site["name"], path)
                    targets = [resolved] if resolved else []
                if not targets:
                    name, quote = _masked(site["name"], site["quote"])
                    facts.append(
                        Fact(
                            "code.matlab",
                            path,
                            "unresolved",
                            {"literal": {"kind": predicate, "name": name}},
                            "T6",
                            _line_locator(site["line"]),
                            quote,
                        )
                    )
                for target in targets:
                    if predicate == "generated_by":  # the output file is the subject
                        facts.append(
                            Fact(
                                "code.matlab",
                                target,
                                "generated_by",
                                {"path": path},
                                "T6",
                                _line_locator(site["line"]),
                                site["quote"],
                            )
                        )
                    else:
                        facts.append(
                            Fact(
                                "code.matlab",
                                path,
                                predicate,
                                {"path": target},
                                "T6",
                                _line_locator(site["line"]),
                                site["quote"],
                            )
                        )
    return facts


def _package_links(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    by_uuid: dict[str, list[str]] = defaultdict(list)
    for (analyzer, path), out in sorted(corpus.outputs.items()):
        if analyzer != "opc.simulink":
            continue
        for key in ("uuid", "release", "revision", "created", "modified", "model_name", "version"):
            if out.get(key):
                facts.append(
                    Fact(
                        "opc.simulink",
                        path,
                        "facet",
                        {"facet": f"package.{key}", "literal": out[key]},
                        "T1",
                        {"kind": "part", "part": "metadata"},
                    )
                )
        is_cache = (out.get("content_type") or "").endswith("simulinkcache")
        if out.get("uuid") and not is_cache:
            by_uuid[out["uuid"]].append(path)
        if is_cache and out.get("model_name"):
            for target in corpus.models(out["model_name"]):
                facts.append(Fact("opc.simulink", path, "cache_of", {"path": target}, "T3"))
        for ref in out.get("model_references", []):
            targets = corpus.models(ref)
            if not targets:
                facts.append(
                    Fact(
                        "opc.simulink",
                        path,
                        "unresolved",
                        {"literal": {"kind": "model_reference", "name": ref}},
                        "T3",
                    )
                )
            for target in targets:
                facts.append(Fact("opc.simulink", path, "model_reference", {"path": target}, "T3"))
        for lib in out.get("library_links", []):
            facts.append(
                Fact("opc.simulink", path, "library_link", {"external": f"simulink:{lib}"}, "T3")
            )
    for members in by_uuid.values():
        if len(members) > 1:
            first, *rest = sorted(members)
            for other in rest:
                facts.append(Fact("opc.simulink", other, "same_origin_as", {"path": first}, "T1"))
    return facts


def _mat_facets(corpus: Corpus) -> list[Fact]:
    facts: list[Fact] = []
    for (analyzer, path), out in sorted(corpus.outputs.items()):
        if analyzer != "data.mat":
            continue
        for key in ("version", "platform", "created"):
            if out.get(key):
                facts.append(
                    Fact(
                        "data.mat",
                        path,
                        "facet",
                        {"facet": f"mat.{key}", "literal": out[key]},
                        "T1",
                        {"kind": "byte", "start": 0, "end": 128},
                    )
                )
        if out.get("variables"):
            names = [v["name"] for v in out["variables"]]
            facts.append(
                Fact("data.mat", path, "facet", {"facet": "mat.variables", "literal": names}, "T1")
            )
    return facts


def _statements(corpus: Corpus) -> list[Fact]:
    """A note line that names an inventory file and carries a status word becomes a located
    statement (T4) with the line as its verbatim quote; the note's own frontmatter status and
    date are facets. Statements are kept as they are: contradictions are resolved by fusion,
    never here."""
    facts: list[Fact] = []
    for (analyzer, path), out in sorted(corpus.outputs.items()):
        if analyzer != "text.statements":
            continue
        front = out.get("frontmatter", {})
        if out.get("hidden_instructions"):
            facts.append(
                Fact(
                    "text.statements",
                    path,
                    "facet",
                    {"facet": "note.hidden_instructions", "literal": out["hidden_instructions"]},
                    "T1",
                )
            )
        if front.get("status"):
            facts.append(
                Fact(
                    "text.statements",
                    path,
                    "facet",
                    {"facet": "note.status", "literal": front["status"].lower()},
                    "T4",
                )
            )
        dated = front.get("updated") or front.get("date")
        for entry in out.get("lines", []):
            line, text = entry["line"], entry["text"]
            words = [m.group(0).lower() for m in corpus.status_regex.finditer(text)] + [
                m.group(0).lower() for m in _REPLACES.finditer(text)
            ]
            day = _DATE.search(text)
            when = day.group(1) if day else dated
            mentioned = []
            for found in _PATHLIKE.finditer(text):
                target = corpus.resolve_path(found.group(0), path) or corpus.resolve_path(
                    found.group(0), ""
                )
                if target and target != path and target not in mentioned:
                    mentioned.append(target)
            for target in mentioned:
                facts.append(
                    Fact(
                        "text.references",
                        path,
                        "references",
                        {"path": target},
                        "T6",
                        _line_locator(line),
                        text.strip(),
                    )
                )
                status, problem, term = (
                    _status_for(text, target, corpus) if words else (None, None, None)
                )
                reader = (
                    "profile.status_terms"
                    if term is not None and term in corpus.profile_terms
                    else "text.statements"
                )
                if problem is not None:
                    facts.append(
                        Fact(
                            "text.statements",
                            path,
                            "unresolved",
                            {
                                "path": target,
                                "literal": {"kind": "status_statement", "problem": problem},
                            },
                            "T8",
                            _line_locator(line),
                            text.strip(),
                        )
                    )
                if status is None:
                    continue
                facts.append(
                    Fact(
                        reader,
                        path,
                        "has_status",
                        {"path": target, "literal": {"status": status, "date": when}},
                        "T4",
                        _line_locator(line),
                        text.strip(),
                    )
                )
    return facts


_NEGATION = re.compile(
    r"\b(?:not|no|never|neither|nor|without|isn't|aren't|wasn't|weren't)\b|n't\b", re.I
)
_CLAUSE = re.compile(
    r"\s*(?:[;:]|,\s*(?:but|while|whereas|although)\b|\b(?:but|while|whereas|although|though|and)\b)\s*",
    re.I,
)
_BY_NEWER = re.compile(
    r"\b(?:is\s+|was\s+|has\s+been\s+|have\s+been\s+)?(?:superseded|replaced)\s+(?:by|with)\b", re.I
)
_REPLACES = re.compile(r"\b(?:replaces|supersedes|supersede|replace)\b", re.I)


def _status_for(
    text: str, target: str, corpus: Corpus
) -> tuple[str | None, str | None, str | None]:
    """``(status, problem)`` a line gives ``target`` (review cp17-234-a F1). Status words match
    whole words only. Direction is read for "A is superseded/replaced by B" (A superseded, B
    gets nothing) and "B replaces/supersedes A" (A superseded). Otherwise the clause naming
    the target must name no other file and hold no negation; when it does, nothing is
    attributed and the problem is returned, so the line stays an unresolved statement that
    the owner or an agent's reading can settle."""
    names = sorted({target.lower(), target.rsplit("/", 1)[-1].lower()}, key=len, reverse=True)

    def where(segment: str) -> int:
        lower = segment.lower()
        return min((lower.find(n) for n in names if n in lower), default=-1)

    def paths_in(segment: str) -> list[tuple[int, str]]:
        found = []
        for match in _PATHLIKE.finditer(segment):
            resolved = corpus.resolve_path(match.group(0), "") or match.group(0)
            found.append((match.start(), resolved))
        return found

    for sentence in re.split(r"(?<=[.;!?])\s+", text):
        position = where(sentence)
        if position < 0:
            continue
        for pattern, older_side in ((_BY_NEWER, "before"), (_REPLACES, "after")):
            phrase = pattern.search(sentence)
            if phrase is None or len(paths_in(sentence)) < 2:
                continue
            if _NEGATION.search(sentence):
                return None, "a negated replacement", None
            is_before = position < phrase.start()
            older = is_before if older_side == "before" else not is_before
            return ("superseded", None, None) if older else (None, None, None)
        for clause in _CLAUSE.split(sentence):
            if where(clause) < 0:
                continue
            words = list(corpus.status_regex.finditer(clause))
            if not words:
                continue
            if len({p for _, p in paths_in(clause)}) > 1:
                return None, "several files in one clause", None
            spans = [(m.start(), m.end()) for m in words]
            negated = [
                m
                for m in _NEGATION.finditer(clause)
                if not any(s <= m.start() < e for s, e in spans)
            ]
            if negated:
                return None, "a negated status", None
            at = where(clause)
            after = [m for m in words if m.start() > at]
            chosen = after[0] if after else words[-1]
            term = chosen.group(0).lower()
            return corpus.status_words[term], None, term
    return None, None, None


# ----- near duplicates (MinHash over word shingles, same-stem candidates only) ---------------

NEAR_HASHES = 64
NEAR_SHINGLE = 5
NEAR_THRESHOLD = 0.6


def _minhash(text: str) -> list[int]:
    words = re.findall(r"\w+|[^\w\s]", text.lower())
    shingles = {
        " ".join(words[i : i + NEAR_SHINGLE]) for i in range(max(1, len(words) - NEAR_SHINGLE + 1))
    }
    # One hash per shingle, then 64 fixed XOR masks as the permutations (review cp17-234-a F16:
    # 64 keyed hashes per shingle took about 20 s per megabyte).
    hashes = [
        int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in shingles
    ]
    return [min(h ^ mask for h in hashes) for mask in _NEAR_MASKS]


_NEAR_MASKS = [
    int.from_bytes(hashlib.blake2b(f"near-copy-mask-{i}".encode(), digest_size=8).digest(), "big")
    for i in range(NEAR_HASHES)
]


def near_copies(corpus: Corpus, root: Path) -> list[Fact]:
    """Text files that share a family stem but not a digest: their estimated shingle Jaccard
    similarity, when it reaches the threshold (T7: similarity never decides alone)."""
    declaration = ANALYZERS["identity.near_copies"]
    groups: dict[str, list[AdoptionFile]] = defaultdict(list)
    for row in corpus.files():
        if (
            row.container == "text"
            and row.status == "read"
            and (row.size or 0) <= declaration.max_bytes
        ):
            groups[name_stem(row.path)[0]].append(row)
    facts: list[Fact] = []
    for stem, rows in sorted(groups.items()):
        if not stem or len(rows) < 2 or len(rows) > 50:
            continue
        signatures: dict[str, list[int]] = {}
        for row in rows:
            data = read_bytes(root, row, declaration.max_bytes)
            text = decode_text(data) if data is not None else None
            if text:
                signatures[row.path] = _minhash(text)
        paths = sorted(signatures)
        for i, a in enumerate(paths):
            for b in paths[i + 1 :]:
                if corpus.rows[a].text_sha256 == corpus.rows[b].text_sha256:
                    continue  # already an exact or normalised copy
                same = sum(x == y for x, y in zip(signatures[a], signatures[b], strict=True))
                similarity = round(same / NEAR_HASHES, 3)
                if similarity >= NEAR_THRESHOLD:
                    facts.append(
                        Fact(
                            "identity.near_copies",
                            b,
                            "copy_of",
                            {"path": a, "literal": {"level": "near", "similarity": similarity}},
                            "T7",
                        )
                    )
    return facts


# ----- an existing Graphify graph (optional input, never run) ---------------------------------

_GRAPHIFY_CODE_RELATIONS = {"calls": "calls", "imports_from": "references", "imports": "references"}


def graphify_facts(corpus: Corpus, root: Path) -> list[Fact]:
    """Evidence from a ``graphify-out/graph.json`` found in the folder (R4 2.1): an EXTRACTED
    code edge counts (T3) only when both files are fresh, i.e. the MD5 that Graphify's
    ``manifest.json`` recorded equals the file's bytes now; inferred or ambiguous edges are
    hints (T8). The manifest's absolute paths are matched by suffix and never stored. Graphify
    is never run, and its communities are not evidence."""
    facts: list[Fact] = []
    for row in corpus.files():
        if not row.path.endswith("graphify-out/graph.json"):
            continue
        base = row.path[: -len("graphify-out/graph.json")]
        data = read_bytes(root, row, MAX_READ)
        try:
            graph = json.loads(data) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            graph = None
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
            facts.append(
                Fact(
                    "graphify.import",
                    row.path,
                    "unresolved",
                    {
                        "literal": {
                            "kind": "graphify_graph",
                            "problem": "unreadable" if readable(row) else f"not read: {row.status}",
                        }
                    },
                    "T1",
                )
            )
            continue
        meta: dict[str, Any] = graph["graph"] if isinstance(graph.get("graph"), dict) else {}
        version = meta.get("graphify_version") or ("<=0.8" if "built_at_commit" in graph else None)
        facts.append(
            Fact(
                "graphify.import",
                row.path,
                "facet",
                {"facet": "graphify.version", "literal": str(version)[:40]},
                "T1",
            )
        )
        manifest_row = corpus.rows.get(base + "graphify-out/manifest.json")
        manifest: dict[str, Any] = {}
        if manifest_row is not None and manifest_row.sha256:
            raw = read_bytes(root, manifest_row, MAX_READ)
            try:
                loaded = json.loads(raw) if raw is not None else {}
            except (ValueError, UnicodeDecodeError):
                loaded = {}
            manifest = loaded if isinstance(loaded, dict) else {}
        recorded: dict[str, str] = {}
        for key, entry in manifest.items():
            if isinstance(entry, dict) and isinstance(entry.get("ast_hash"), str):
                normal = str(key).replace("\\", "/").lower()
                for path in corpus.by_lower:
                    if path.startswith(base.lower()) and normal.endswith("/" + path[len(base) :]):
                        recorded[corpus.by_lower[path]] = entry["ast_hash"]
        fresh: dict[str, bool] = {}

        files: dict[str, str] = {}
        for node in graph["nodes"]:
            if isinstance(node, dict) and isinstance(node.get("source_file"), str):
                hit = corpus.by_lower.get((base + node["source_file"].replace("\\", "/")).lower())
                if hit:
                    files[str(node.get("id"))] = hit
        links = (
            graph.get("links") if isinstance(graph.get("links"), list) else graph.get("edges", [])
        )
        for edge in links if isinstance(links, list) else []:
            if not isinstance(edge, dict):
                continue
            source, target = files.get(str(edge.get("source"))), files.get(str(edge.get("target")))
            relation = str(edge.get("relation"))
            if source is None or target is None or source == target:
                continue
            if edge.get("confidence") == "EXTRACTED" and relation in _GRAPHIFY_CODE_RELATIONS:
                if _fresh(corpus, root, recorded, fresh, source) and _fresh(
                    corpus, root, recorded, fresh, target
                ):
                    facts.append(
                        Fact(
                            "graphify.import",
                            source,
                            _GRAPHIFY_CODE_RELATIONS[relation],
                            {"path": target},
                            "T3",
                        )
                    )
            elif edge.get("confidence") in ("INFERRED", "AMBIGUOUS") and relation != "contains":
                facts.append(
                    Fact(
                        "graphify.import",
                        source,
                        "references",
                        {"path": target, "literal": {"graphify": relation[:40]}},
                        "T8",
                    )
                )
        for path in sorted(set(files.values())):
            facts.append(
                Fact(
                    "graphify.import",
                    path,
                    "facet",
                    {
                        "facet": "graphify.freshness",
                        "literal": "fresh"
                        if _fresh(corpus, root, recorded, fresh, path)
                        else ("stale" if path in recorded else "unrecorded"),
                    },
                    "T1",
                )
            )
    return facts


def _fresh(
    corpus: Corpus, root: Path, recorded: dict[str, str], seen: dict[str, bool], path: str
) -> bool:
    """Whether Graphify's recorded MD5 for ``path`` equals the file's bytes now (memoised)."""
    if path not in seen:
        row = corpus.rows.get(path)
        data = read_bytes(root, row, MAX_READ) if row is not None and row.sha256 else None
        seen[path] = bool(
            data is not None
            and path in recorded
            and hashlib.md5(data, usedforsecurity=False).hexdigest() == recorded[path]
        )
    return seen[path]


# ----- MATLAB projects (a directory analyzer) ------------------------------------------------


def project_facts(corpus: Corpus, root: Path) -> list[Fact]:
    """Each ``.prj`` with its ``resources/project`` metadata (beside it, or the nearest such
    folder when it is not: reported as unresolved) becomes ``project_file`` evidence per member
    file, labels included; members missing from the inventory are unresolved."""
    facts: list[Fact] = []
    prjs = [r.path for r in corpus.files() if r.path.lower().endswith(".prj")]
    metadata_roots = sorted(
        {
            r.path.split("resources/project/")[0]
            for r in corpus.files()
            if "resources/project/Project.xml" in r.path
        }
    )
    for prj in prjs:
        base = PurePosixPath(prj).parent.as_posix()
        prefix = "" if base == "." else base + "/"
        meta = prefix if prefix in metadata_roots else None
        if meta is None:
            facts.append(
                Fact(
                    "matlab.project",
                    prj,
                    "unresolved",
                    {"literal": {"kind": "project_metadata", "found": metadata_roots[:5]}},
                    "T3",
                )
            )
            if len(metadata_roots) != 1:
                continue
            meta = metadata_roots[0]
        files: dict[str, bytes] = {}
        unread: list[str] = []
        folder = f"{meta}resources/project/"
        for row in corpus.files():
            if not row.path.startswith(folder):
                continue
            if not readable(row):  # a placeholder is never opened (review cp17-234-a F2)
                unread.append(row.path[len(folder) :])
            elif row.size is not None and row.size < 1024 * 1024:
                data = read_bytes(root, row, 1024 * 1024)
                if data is not None:
                    files[row.path[len(folder) :]] = data
        if unread:
            facts.append(
                Fact(
                    "matlab.project",
                    prj,
                    "unresolved",
                    {"literal": {"kind": "project_metadata", "not_read": sorted(unread)[:20]}},
                    "T3",
                )
            )
        info = matlab.read_project(files)
        for problem in info.problems:
            facts.append(
                Fact(
                    "matlab.project",
                    prj,
                    "unresolved",
                    {"literal": {"kind": "project_metadata", "problem": problem[:200]}},
                    "T3",
                )
            )
        for entry in info.entries:
            member = corpus.by_lower.get(f"{meta}{entry.path}".lower())
            if member is None:
                facts.append(
                    Fact(
                        "matlab.project",
                        prj,
                        "unresolved",
                        {"literal": {"kind": "project_member", "name": entry.path[:300]}},
                        "T3",
                    )
                )
                continue
            facts.append(
                Fact(
                    "matlab.project",
                    member,
                    "project_file",
                    {"path": prj, "literal": {"label": entry.label}},
                    "T3",
                )
            )
    for row in corpus.files():
        if row.path.lower().endswith("matlab.toml"):
            facts.append(
                Fact(
                    "matlab.project",
                    row.path,
                    "facet",
                    {"facet": "project.kind", "literal": "matlab.toml"},
                    "T3",
                )
            )
    return facts


# ----- reading -------------------------------------------------------------------------------


def readable(row: AdoptionFile) -> bool:
    """Whether the inventory read this file itself: only such a file may be opened by an
    analyzer. A placeholder, an excluded or unreadable entry, a link or an archive member is
    never opened (review cp17-234-a F2: a cloud placeholder would be downloaded)."""
    return row.entry == "file" and row.status in ("read", "limited") and row.sha256 is not None


def read_bytes(root: Path, row: AdoptionFile, limit: int) -> bytes | None:
    """The row's bytes (read-only), or ``None`` when the inventory did not read the file, it
    became a link or a placeholder, or its bytes no longer match the inventory digest (the
    folder changed). Every analyzer read goes through here. ``limit`` caps what is read; a
    capped read is not re-hashed (it can only be a prefix)."""
    if not readable(row):
        return None
    target = os_path(root / disk_path(row.path))
    try:
        before = os.lstat(target)
        attributes = int(getattr(before, "st_file_attributes", 0))
        tag = int(getattr(before, "st_reparse_tag", 0))
        if kind_of(before.st_mode, attributes, tag) != "file" or attributes & PLACEHOLDER_BITS:
            return None
        with open(target, "rb") as handle:
            after = os.fstat(handle.fileno())
            if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
                return None
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) <= limit:
        if hashlib.sha256(data).hexdigest() != row.sha256:
            return None
        return data
    return data[:limit]


def extract(analyzer: str, root: Path, row: AdoptionFile) -> dict[str, Any] | None:
    declaration = ANALYZERS[analyzer]
    data = read_bytes(root, row, declaration.max_bytes)
    if data is None:
        return None
    return EXTRACTORS[analyzer](row.path, data, row)


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def output_digest(outputs: dict[tuple[str, str], dict[str, Any]]) -> str:
    return hashlib.sha256(
        dumps(sorted((list(k), v) for k, v in outputs.items())).encode()
    ).hexdigest()
