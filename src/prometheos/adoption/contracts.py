"""Exported adoption contracts (A05, CP17.1; decision 0039 sections 2-3).

Each storage row model's fields are its table's columns (``prometheos.adoption.tables`` pins
that). JSON columns are validated by these models on write and on read. Paths are always
relative to the adoption root, in POSIX form. An archive member is ``<archive>!/<member>``;
every ``!`` inside a name is written ``!!`` (on disk and inside archives alike), so ``!/`` is
always and only the step into an archive (review cp17-1-a F1).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from prometheos.domain.ids import OperationRunId
from prometheos.domain.models import MAX_INT, Sha256, StrictModel, Timestamp

Count = Annotated[int, Field(ge=0, le=MAX_INT)]
ScanGen = Annotated[int, Field(ge=1, le=MAX_INT)]
RootAlias = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Glob = Annotated[str, StringConstraints(min_length=1, max_length=256)]

MEMBER_SEPARATOR = "!/"
MAX_PATH_CHARS = 32_767  # the longest Windows path
PLACEHOLDER_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000  # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def escape_segment(name: str) -> str:
    """A file or member name as a path segment: ``!`` doubled."""
    return name.replace("!", "!!")


def split_path(value: str) -> list[str]:
    """The levels of a path, unescaped: ``[path on disk, member path, inner member path...]``.
    ``!!`` is a literal ``!``; ``!/`` steps into an archive; any other ``!`` is invalid."""
    levels: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "!":
            nxt = value[i + 1] if i + 1 < len(value) else ""
            if nxt == "!":
                current.append("!")
                i += 2
                continue
            if nxt == "/":
                levels.append("".join(current))
                current = []
                i += 2
                continue
            raise ValueError("a '!' in a path is written '!!' (or '!/' to step into an archive)")
        current.append(ch)
        i += 1
    levels.append("".join(current))
    return levels


def disk_path(value: str) -> str:
    """The on-disk part of a path, unescaped (what to open under the root)."""
    return split_path(value)[0]


def member_path(parent: str, name: str) -> str:
    """The path of member ``name`` (already sanitised, ``/``-separated) inside ``parent``."""
    return parent + MEMBER_SEPARATOR + "/".join(escape_segment(s) for s in name.split("/"))


def check_relative(value: str) -> str:
    """A root-relative POSIX path (archive levels joined by ``!/``): no drive, no leading
    slash, no backslash, no control character, no empty, ``.`` or ``..`` segment."""
    if not value or len(value) > MAX_PATH_CHARS:
        raise ValueError(f"path is empty or longer than {MAX_PATH_CHARS} characters")
    if "\\" in value or _CONTROL.search(value) or _DRIVE.match(value) or value.startswith("/"):
        raise ValueError("path must be relative to the adoption root, in POSIX form")
    for level in split_path(value):
        for part in level.split("/"):
            if part in ("", ".", ".."):
                raise ValueError("path has an empty, '.' or '..' segment")
    return value


def _bounded(value: Any, depth: int = 0) -> Any:
    """Public JSON stays within exact browser integers and a sane nesting depth."""
    if depth > 16:
        raise ValueError("value nested too deeply")
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        if not -MAX_INT <= value <= MAX_INT:
            raise ValueError("integer outside the exact public range (2^53)")
        return value
    if isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1)
        return value
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1)
        return value
    return value


BoundedJson = Annotated[JsonValue, AfterValidator(_bounded)]
RelPath = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATH_CHARS)]

FileStatus = Literal[
    "read", "listed", "placeholder", "excluded", "noise", "link", "unreadable", "limited"
]
FILE_STATUSES: tuple[str, ...] = FileStatus.__args__  # type: ignore[attr-defined]

Container = Literal[
    "text",
    "binary",
    "zip",
    "opc_slx",
    "opc_mlx",
    "opc_slxc",
    "opc_office",
    "opc_other",
    "mat_v4",
    "mat_v5",
    "mat_v73",
    "hdf5",
    "pdf",
    "png",
    "jpeg",
    "executable",
    "empty",
    "unknown",
]
CONTAINERS: tuple[str, ...] = Container.__args__  # type: ignore[attr-defined]

Zone = Literal[
    "source_code",
    "notebooks",
    "data_raw",
    "data_derived",
    "results",
    "models_sim",
    "manuscript",
    "deliverable",
    "notes",
    "literature",
    "instrument_data",
    "tracking_records",
    "agent_memory",
    "tool_output",
    "env_cache_noise",
    "vendored",
    "backup_copy",
    "archive",
    "unknown",
]
ZONES: tuple[str, ...] = Zone.__args__  # type: ignore[attr-defined]

Entry = Literal["file", "folder", "link", "member"]
Change = Literal["new", "modified", "unchanged", "moved", "restored", "metadata_only"]
ScanState = Literal["running", "complete", "failed", "pruned"]  # pruned: rows dropped (0045)
Tier = Literal["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]
TIERS: tuple[str, ...] = Tier.__args__  # type: ignore[attr-defined]

LimitName = Literal[
    "max_members", "max_depth", "max_member_bytes", "max_total_uncompressed", "max_ratio"
]


# ----- scan configuration and summary --------------------------------------------------------


class ArchiveLimits(StrictModel):
    """Per-archive listing limits (decision 0039 section 4): beyond one, the archive or member
    is recorded as ``limited`` with the limit's name, and nothing more of it is read."""

    max_members: int = Field(default=10_000, ge=1, le=1_000_000)
    max_depth: int = Field(default=2, ge=0, le=8)
    max_member_bytes: int = Field(default=64 * 1024 * 1024, ge=0, le=2**30)
    max_total_uncompressed: int = Field(default=2 * 1024**3, ge=0, le=2**36)
    max_ratio: int = Field(default=200, ge=1, le=100_000)


class ScanConfig(StrictModel):
    exclusions: list[Glob] = Field(default_factory=list, max_length=200)
    limits: ArchiveLimits = Field(default_factory=ArchiveLimits)
    profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")] = "generic"
    previous_gen: ScanGen | None = None
    rank: bool = True  # False: inventory and analyzers only (a real folder before blind labels)


class ScanSummary(StrictModel):
    entries: Count
    files_on_disk: Count
    members: Count
    bytes_on_disk: Count
    by_status: dict[FileStatus, Count]
    by_container: dict[Container, Count]
    by_zone: dict[Zone, Count]
    unreadable: Count
    limited: Count
    seconds: Annotated[float, Field(ge=0, le=10**9)]


class AnalyzerUnavailable(StrictModel):
    """An analyzer this installation cannot run, why, and how to add it (decision 0053)."""

    analyzer: AnalyzerId
    reason: ShortText
    install: ShortText
    files: Count = 0  # files of the generation it would have read


class AnalysisSummary(StrictModel):
    """What the analyzer pack produced for a generation (CP17.2)."""

    evidence: Count
    by_predicate: dict[str, Count]
    extracted: Count
    replayed: Count
    changed_since_inventory: Count
    families: Count = 0
    by_band: dict[str, Count] = Field(default_factory=dict)
    conflicts: Count = 0
    analyzers_unavailable: list[AnalyzerUnavailable] = Field(default_factory=list, max_length=20)


class AdoptionScan(StrictModel):
    """One scan generation of one root (table ``adoption_scans``)."""

    scan_gen: ScanGen
    root_alias: RootAlias
    run_id: OperationRunId | None
    state: ScanState
    config: ScanConfig
    started_at: Timestamp
    finished_at: Timestamp | None
    root_digest_before: Sha256 | None
    root_digest_after: Sha256 | None
    summary: ScanSummary | None
    error: Annotated[str, StringConstraints(max_length=2000)] | None = None
    analysis: AnalysisSummary | None = None

    @model_validator(mode="after")
    def _complete(self) -> AdoptionScan:
        if self.state == "complete":
            if self.summary is None or self.finished_at is None:
                raise ValueError("a complete scan has a summary and a finish time")
            if self.root_digest_after is None or self.root_digest_before is None:
                raise ValueError("a complete scan has both root digests")
            if self.root_digest_before != self.root_digest_after:
                raise ValueError("a complete scan has equal root digests (nothing was written)")
        if self.state == "running" and self.finished_at is not None:
            raise ValueError("a running scan has no finish time")
        return self


# ----- inventory rows ------------------------------------------------------------------------


class AdoptionFile(StrictModel):
    """One inventory row (table ``adoption_files``)."""

    scan_gen: ScanGen
    path: RelPath
    parent: RelPath | None
    depth: Annotated[int, Field(ge=0, le=8)]
    entry: Entry
    size: Count | None
    mtime_us: Annotated[int, Field(ge=-MAX_INT, le=MAX_INT)] | None
    attributes: Annotated[int, Field(ge=0, le=2**32 - 1)] | None
    sha256: Sha256 | None
    text_sha256: Sha256 | None
    container: Container | None
    status: FileStatus
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None
    zone: Zone
    change: Change | None
    detail: dict[str, BoundedJson] | None

    @field_validator("path", "parent")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return None if value is None else check_relative(value)

    @model_validator(mode="after")
    def _consistent(self) -> AdoptionFile:
        levels = len(split_path(self.path)) - 1
        if self.entry == "member":
            if self.parent is None or not self.path.startswith(self.parent + MEMBER_SEPARATOR):
                raise ValueError("an archive member's path starts with its archive's path and '!/'")
            if levels < 1 or self.depth != levels:
                raise ValueError("an archive member's depth is its number of archive levels")
            if len(split_path(self.parent)) != levels:
                raise ValueError("an archive member's parent is one level up")
        elif self.parent is not None or self.depth != 0 or levels != 0:
            raise ValueError("an entry on disk has no parent archive, depth 0 and no '!/'")
        if (self.entry == "link") != (self.status == "link"):
            raise ValueError("a link entry has status link, and only a link entry does")
        if self.entry == "folder" and self.status not in (
            "noise", "excluded", "placeholder", "unreadable"
        ):  # fmt: skip
            raise ValueError("a folder row summarises a folder that was not listed")
        if self.status == "read" and (self.attributes or 0) & PLACEHOLDER_ATTRIBUTES:
            raise ValueError("a cloud placeholder is never read")
        if self.status == "read" and self.sha256 is None:
            raise ValueError("a read row carries its content digest")
        if self.status in ("placeholder", "excluded", "noise", "link") and (
            self.sha256 is not None or self.text_sha256 is not None
        ):
            raise ValueError(f"a {self.status} row is never opened, so it has no digest")
        if self.status in ("unreadable", "limited") and self.reason is None:
            raise ValueError(f"an {self.status} row says why")
        if self.text_sha256 is not None and self.container != "text":
            raise ValueError("only text rows carry a normalised-text digest")
        return self


# ----- analyzers, evidence, profiles (CP17.2-CP17.3 fill them) ------------------------------

AnalyzerId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")]
AnalyzerKind = Literal["builtin", "declarative", "pinned_tool", "agent_task", "ai_task"]
Determinism = Literal["pure", "pinned_tool", "reported", "proposed"]

Predicate = Literal[
    "copy_of",
    "same_origin_as",
    "revision_of",
    "autosave_of",
    "backup_of",
    "cache_of",
    "derived_from",
    "generated_by",
    "used",
    "consumed_by",
    "references",
    "calls",
    "model_reference",
    "library_link",
    "has_status",
    "supersedes",
    "project_file",
    "facet",
    "language",
    "unresolved",
]
PREDICATES: tuple[str, ...] = Predicate.__args__  # type: ignore[attr-defined]


class AnalyzerSelect(StrictModel):
    containers: list[Container] = Field(default_factory=list, max_length=len(CONTAINERS))
    extensions: list[Annotated[str, StringConstraints(pattern=r"^\.[a-z0-9.]{1,20}$")]] = Field(
        default_factory=list, max_length=50
    )
    zones: list[Zone] = Field(default_factory=list, max_length=len(ZONES))


class AdoptionAnalyzer(StrictModel):
    """An analyzer declaration: a typed, versioned producer of evidence. Declarations are data;
    PrometheOS never runs third-party code for an analyzer (spec/02, decision 0039)."""

    id: AnalyzerId
    version: SemVer
    kind: AnalyzerKind
    determinism: Determinism
    summary: ShortText
    selects: AnalyzerSelect
    reads: Literal["inventory_only", "prefix", "container_parts", "whole_file", "directory"]
    max_bytes: Annotated[int, Field(ge=0, le=2**40)]
    emits: list[Predicate] = Field(min_length=1, max_length=len(PREDICATES))
    tiers: list[Tier] = Field(min_length=1, max_length=9)
    hydrates_placeholders: Literal[False] = False

    @model_validator(mode="after")
    def _kinds(self) -> AdoptionAnalyzer:
        expected = {
            "builtin": "pure",
            "declarative": "pure",
            "pinned_tool": "pinned_tool",
            "agent_task": "reported",
            "ai_task": "proposed",
        }[self.kind]
        if self.determinism != expected:
            raise ValueError(f"a {self.kind} analyzer is {expected}")
        if self.kind == "ai_task" and set(self.tiers) - {"T4", "T8"}:
            raise ValueError("AI output is a quote-verified statement (T4) or a hint (T8) only")
        if "T0" in self.tiers:
            raise ValueError("T0 is the owner's decision; no analyzer produces it")
        return self


LocatorKind = Literal["line", "byte", "part", "xml", "json_pointer", "page_span", "member"]


class Locator(StrictModel):
    kind: LocatorKind
    part: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    start: Count | None = None
    end: Count | None = None
    pointer: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def _span(self) -> Locator:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("a locator ends at or after its start")
        return self


class EvidenceObject(StrictModel):
    """Exactly one of: another inventory path, a literal value, an external identifier, or a
    facet name with its value."""

    path: RelPath | None = None
    literal: BoundedJson = None
    external: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    facet: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")] | None = None

    @model_validator(mode="after")
    def _one(self) -> EvidenceObject:
        given = [self.path is not None, self.external is not None, self.facet is not None]
        if sum(given) > 1 or (sum(given) == 0 and self.literal is None):
            raise ValueError("an evidence object is one path, one external id, or one facet")
        if self.path is not None:
            check_relative(self.path)
        return self


class AdoptionEvidence(StrictModel):
    """One fact from one analyzer about one inventory row (table ``adoption_evidence``)."""

    evidence_id: Annotated[str, StringConstraints(pattern=r"^ev_[0-9a-f]{32}$")]
    scan_gen: ScanGen
    analyzer: AnalyzerId
    analyzer_version: SemVer
    subject: RelPath
    subject_sha256: Sha256 | None
    predicate: Predicate
    object: EvidenceObject
    locator: Locator | None
    quote: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    quote_sha256: Sha256 | None
    tier: Tier
    determinism: Determinism

    @field_validator("subject")
    @classmethod
    def _relative(cls, value: str) -> str:
        return check_relative(value)

    @model_validator(mode="after")
    def _quote(self) -> AdoptionEvidence:
        if (self.quote is None) != (self.quote_sha256 is None):
            raise ValueError("a quote and its digest come together")
        if self.tier == "T0":
            raise ValueError("T0 evidence is an owner decision, not analyzer evidence")
        if self.determinism == "proposed" and self.tier not in ("T4", "T8"):
            raise ValueError("proposed (AI) evidence is T4 with a verified quote, or T8")
        if self.determinism == "proposed" and self.tier == "T4" and self.quote is None:
            raise ValueError("T4 evidence from an AI task needs a verified quote")
        return self


RankingRule = Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]


# ----- families and ranks (CP17.3; decision 0041) -------------------------------------------

EvidenceId = Annotated[str, StringConstraints(pattern=r"^ev_[0-9a-f]{32}$")]
RuleName = Literal["R1", "R2", "R3", "R3b", "R4", "R5", "R6", "R7", "R8"]
Band = Literal["decided", "supported", "weak", "undecided"]
FamilyKind = Literal[
    "code", "notebook", "model", "data", "document", "image", "archive", "other", "mixed"
]
DECIDING = frozenset({"R1", "R2", "R3", "R4"})


class RankReason(StrictModel):
    rule: RuleName
    tier: Tier
    text: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    evidence: list[EvidenceId] = Field(max_length=50)


class FamilyMember(StrictModel):
    path: RelPath
    outcome: Literal["current", "candidate", "excluded", "copy"]
    copy_of: RelPath | None
    rank: Annotated[int, Field(ge=1, le=100_000)] | None
    reasons: list[RankReason] = Field(max_length=50)

    @model_validator(mode="after")
    def _copy(self) -> FamilyMember:
        if (self.outcome == "copy") != (self.copy_of is not None):
            raise ValueError("a copy names the member it copies, and only a copy does")
        if (self.outcome == "copy") != (self.rank is None):
            raise ValueError("copies are not ranked; every other member is")
        return self


class FamilyConflict(StrictModel):
    kind: Literal[
        "statement_conflict",
        "contradicted_statement",
        "owner_overrode_exclusion",
        "old_member_in_use",
    ]
    text: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    evidence: list[EvidenceId] = Field(max_length=100)
    paths: list[RelPath] = Field(max_length=20)


class AdoptionFamily(StrictModel):
    """Files that are versions or copies of one thing, ranked (table ``adoption_families``,
    column ``payload``). Ranks are proposals: only an owner decision (R1, T0) or an accepted
    baseline makes one authoritative."""

    scan_gen: ScanGen
    family_id: Annotated[str, StringConstraints(pattern=r"^fam_[0-9a-f]{16}$")]
    key: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: FamilyKind
    members: list[FamilyMember] = Field(min_length=2, max_length=5000)
    current: RelPath | None
    band: Band
    deciding_rule: RuleName | None
    conflicts: list[FamilyConflict] = Field(max_length=200)

    @model_validator(mode="after")
    def _ranked(self) -> AdoptionFamily:
        paths = [m.path for m in self.members]
        if len(set(paths)) != len(paths):
            raise ValueError("a member appears once")
        currents = [m.path for m in self.members if m.outcome == "current"]
        if self.current is None:
            if currents or self.band != "undecided" or self.deciding_rule is not None:
                raise ValueError("a family without a current member is undecided, with no rule")
        else:
            if currents != [self.current]:
                raise ValueError("the current member is the one member marked current")
            if self.band == "undecided" or self.deciding_rule is None:
                raise ValueError("a current member has a band and the rule that chose it")
            if self.band == "decided" and self.deciding_rule not in DECIDING:
                raise ValueError("only R1-R4 decide; later rules support or are weak")
        known = set(paths)
        for m in self.members:
            if m.copy_of is not None and m.copy_of not in known:
                raise ValueError("a copy points at a member of the same family")
        return self


StatusValue = Literal[
    "current",
    "superseded",
    "invalidated",
    "withdrawn",
    "draft",
    "final_claimed",
    "archived",
    "failed",
    "unknown",
]


class Marker(StrictModel):
    glob: Glob
    weight: Annotated[int, Field(ge=1, le=100)]


class Vocabulary(StrictModel):
    """A profile's words (declarative analyzers, review cp17-234-a F14). No regular expressions:
    a status term is plain words matched whole; a version token is letters, digits, ``_`` or
    ``-``, where ``#`` stands for a number (``rev#`` matches ``rev12``). Both compile to bounded
    patterns that cannot backtrack badly."""

    status_terms: dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z' \-]{0,38}[A-Za-z]$")],
        StatusValue,
    ] = Field(default_factory=dict, max_length=100)
    version_tokens: list[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{0,20}#?[A-Za-z0-9_\-]{0,20}$")]
    ] = Field(default_factory=list, max_length=50)

    @field_validator("status_terms")
    @classmethod
    def _terms(cls, value: dict[str, str]) -> dict[str, str]:
        return {term.lower(): status for term, status in value.items()}  # matched ignoring case

    @field_validator("version_tokens")
    @classmethod
    def _tokens(cls, value: list[str]) -> list[str]:
        if any(not token.strip("#") for token in value):
            raise ValueError("a version token needs at least one letter or digit")
        return [token.lower() for token in value]  # matched ignoring case


class AdoptionProfile(StrictModel):
    """An adoption profile: declarative preset of markers, zones, noise, analyzers, vocabulary
    and ranking rule order (R4 3.6). Profiles are data, stored in the PrometheOS store or
    shipped with the release, never in the adopted tree."""

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    version: SemVer
    extends: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")] | None
    summary: ShortText
    markers: list[Marker] = Field(default_factory=list, max_length=50)
    threshold: Annotated[int, Field(ge=0, le=1000)] = 0
    zones: dict[Zone, list[Glob]] = Field(default_factory=dict)
    noise: list[Glob] = Field(default_factory=list, max_length=200)
    analyzers: list[AnalyzerId] = Field(default_factory=list, max_length=100)
    vocabulary: Vocabulary
    ranking: list[RankingRule] = Field(min_length=1, max_length=8)

    @field_validator("ranking")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("a ranking rule appears once")
        if value[0] != "R1":
            raise ValueError("the owner's decision (R1) always ranks first")
        return value


# ----- baseline package (CP17.6; decision 0045) ----------------------------------------------

BaselineId = Annotated[str, StringConstraints(pattern=r"^bl_[0-9a-f]{24}$")]


class PackagedBaseline(StrictModel):
    baseline_id: BaselineId
    sha256: Sha256
    payload: dict[str, JsonValue]
    accepted_at: Timestamp
    accepted_by: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    plan_id: Annotated[str, StringConstraints(pattern=r"^plan_[0-9a-f]{16}$")]
    plan_version: Annotated[int, Field(ge=1, le=MAX_INT)]


class PackagedDecision(StrictModel):
    family_id: Annotated[str, StringConstraints(pattern=r"^fam_[0-9a-f]{16}$")]
    current_path: RelPath
    current_sha256: Sha256 | None
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    decided_by: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    decided_at: Timestamp


class PackagedClassDecision(StrictModel):
    """A06.1 (CP20.4; decision 0106, review cp20-4-a F5): an owner's class decision, by family
    key and exact member set, as in ``adoption_class_decisions``."""

    family_key: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    members: Annotated[str, StringConstraints(min_length=2, max_length=2_000_000)]
    decision: Literal["variant", "noise", "split", "revoked"]
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    decided_by: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    decided_at: Timestamp


class AdoptionBaselinePackage(StrictModel):
    """An accepted baseline with its generation and the folder's owner decisions, as
    ``adoption_baseline_export`` returns it and ``adoption_baseline_import`` takes it.
    ``sha256`` is the SHA-256 of the canonical content without it (checked on import)."""

    kind: Literal["prometheos-adoption-baseline"]
    format: Literal[1]
    baseline: PackagedBaseline
    scan: AdoptionScan
    files: list[AdoptionFile] = Field(max_length=2_000_000)
    evidence: list[AdoptionEvidence] = Field(max_length=5_000_000)
    decisions: list[PackagedDecision] = Field(max_length=100_000)
    # Absent from packages made before CP20.4 6c, so their digests are unchanged.
    class_decisions: list[PackagedClassDecision] = Field(
        default_factory=list, max_length=100_000, exclude_if=lambda v: not v
    )
    sha256: Sha256

    @model_validator(mode="after")
    def _one_generation(self) -> AdoptionBaselinePackage:
        if self.scan.state != "complete":
            raise ValueError("a baseline's generation is complete")
        gen = self.scan.scan_gen
        if any(f.scan_gen != gen for f in self.files) or any(
            e.scan_gen != gen for e in self.evidence
        ):
            raise ValueError("every row belongs to the baseline's generation")
        return self


class AdoptionBaselineImport(StrictModel):
    """What importing a package created (``adoption_baseline_import``)."""

    baseline_id: BaselineId
    scan_gen: ScanGen
    root_alias: RootAlias
    files: Count
    evidence: Count
    decisions: Count
    families: Count
    imported_by: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    imported_at: Timestamp
