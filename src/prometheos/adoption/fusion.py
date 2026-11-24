"""Fusion and ranking (A05, CP17.3; decision 0041; C17-LINEAGE).

Families are groups of files that are versions or copies of one thing; each family gets a
ranked current member with the reasons, the evidence behind each reason, and the
contradictions that remain. The method is R4 3.3 made concrete:

- **Families** join files linked by T1 identity (equal or normalised content, one package
  uuid), T2 tool conventions (autosave, release-upgrade backup) and, as the weakest link, one
  name stem within one kind of file (code, model, data, ...). Caches, project metadata, agent
  memory and noise never form families.
- **Copies** (equal or normalised content) are one candidate; the copy outside backup folders
  represents it.
- **The rule chain** decides in order, and the first rule that singles out one candidate
  decides: R1 the owner's decision (T0); R2 exclusions (autosaves, upgrade backups, files a
  later statement calls superseded or invalid); R3 identity order inside one origin (package
  revision and release, T1); R3b produced by the current version of its producer (T6, needs a
  corroborator to be more than supported); R4 used or referenced by a current file outside the
  family (T3; T6 literals corroborate only); R5 the latest written statement (T4); R7 embedded
  creation or modification time (T1, never decisive alone: weak); R8 version numbers in names
  (T7, weak). Filesystem modification times decide nothing. ``FINAL`` and ``BEST`` in a name
  rank nothing up.
- **Bands:** ``decided`` (R1, R3, R4 with T1-T3 evidence), ``supported`` (R3b, R5, or a weak
  rule that two independent rules agree on), ``weak`` (R7 or R8 alone), ``undecided``.
- **Contradictions are kept:** a statement that disagrees with the current member, two
  statements of the same date that disagree, and a weaker rule pointing elsewhere are recorded
  on the family, never dropped. Nothing here applies a supersession: ranks are proposals until
  the owner accepts a baseline (CP17.5).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from prometheos.adoption.analyzers import name_stem
from prometheos.adoption.contracts import (
    AdoptionEvidence,
    AdoptionFamily,
    AdoptionFile,
    disk_path,
)

FAMILY_STATUSES = ("read", "placeholder", "limited", "unreadable")
NOT_IN_FAMILIES = frozenset({"env_cache_noise", "agent_memory", "tool_output", "vendored"})
GENERIC_STEMS = frozenset(
    {
        "readme",
        "index",
        "main",
        "notes",
        "todo",
        "license",
        "changelog",
        "untitled",
        "test",
        "tests",
        "data",
        "results",
        "output",
        "config",
        "settings",
        "requirements",
        "about",
        "fixes",
        "summary",
        "report",
        "figure",
        "plot",
        "temp",
        "tmp",
        "new",
        "copy",
    }
)
KINDS: dict[str, str] = {
    **dict.fromkeys(
        (
            ".m",
            ".asv",
            ".py",
            ".r",
            ".jl",
            ".c",
            ".cpp",
            ".h",
            ".java",
            ".js",
            ".ts",
            ".sh",
            ".ps1",
            ".f90",
        ),
        "code",
    ),
    **dict.fromkeys((".mlx", ".ipynb", ".rmd", ".qmd"), "notebook"),
    **dict.fromkeys((".slx", ".mdl", ".sldd", ".mo"), "model"),
    **dict.fromkeys(
        (
            ".mat",
            ".csv",
            ".tsv",
            ".h5",
            ".hdf5",
            ".nc",
            ".parquet",
            ".xlsx",
            ".xls",
            ".npz",
            ".json",
        ),
        "data",
    ),
    **dict.fromkeys(
        (".md", ".txt", ".tex", ".docx", ".doc", ".pdf", ".rst", ".bib", ".pptx", ".odt"),
        "document",
    ),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".svg", ".fig", ".eps", ".tif", ".tiff"), "image"),
    **dict.fromkeys((".zip", ".7z", ".tar", ".gz", ".tgz"), "archive"),
}
_TOOL_SUFFIX = re.compile(r"\.(slx|mdl|sldd)\.r\d{4}[ab]$", re.IGNORECASE)
NEGATIVE_TOKENS = frozenset({"old", "backup", "bak", "copy", "draft"})
NEGATIVE_FOLDERS = frozenset({"old", "legacy", "backup", "backups", "bak", "archive", "archived"})
STATUS_EXCLUDES = frozenset({"superseded", "invalidated", "withdrawn", "failed"})
STATUS_CURRENT = frozenset({"current", "final_claimed"})


def kind_of(path: str) -> str:
    name = PurePosixPath(disk_path(path)).name.lower()
    tool = _TOOL_SUFFIX.search(name)
    if tool:
        return "model"
    return KINDS.get(PurePosixPath(name).suffix, "other")


# ----- evidence index ------------------------------------------------------------------------


@dataclass
class Index:
    rows: dict[str, AdoptionFile]
    evidence: list[AdoptionEvidence]
    out: dict[str, list[AdoptionEvidence]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[str, list[AdoptionEvidence]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        for e in self.evidence:
            self.out[e.subject].append(e)
            if e.object.path is not None:
                self.incoming[e.object.path].append(e)

    def facet(self, path: str, name: str) -> tuple[Any, AdoptionEvidence] | None:
        for e in self.out.get(path, []):
            if e.predicate == "facet" and e.object.facet == name:
                return e.object.literal, e
        return None

    def related(self, path: str, predicate: str) -> list[AdoptionEvidence]:
        return [e for e in self.out.get(path, []) if e.predicate == predicate]

    def statements_about(self, path: str) -> list[AdoptionEvidence]:
        return [e for e in self.incoming.get(path, []) if e.predicate == "has_status"]


# ----- families ------------------------------------------------------------------------------


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def candidates(rows: dict[str, AdoptionFile]) -> list[str]:
    return sorted(
        p
        for p, r in rows.items()
        if r.entry == "file"
        and r.status in FAMILY_STATUSES
        and r.zone not in NOT_IN_FAMILIES
        and "resources/project/" not in disk_path(p)
    )


def families(index: Index) -> list[list[str]]:
    """Groups of two or more candidate files (sorted), joined by identity, conventions and
    name stems."""
    files = candidates(index.rows)
    member = set(files)
    union = _Union()
    for path in files:
        union.find(path)
    for e in index.evidence:
        if e.predicate in ("copy_of", "same_origin_as", "autosave_of", "backup_of"):
            target = e.object.path
            if target in member and e.subject in member:
                union.join(e.subject, target)
    by_stem: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in files:
        stem = stem_of(index, path)
        if len(stem) >= 3 and stem not in GENERIC_STEMS:
            by_stem[(kind_of(path), stem)].append(path)
    for paths in by_stem.values():
        for other in paths[1:]:
            union.join(paths[0], other)
    groups: dict[str, list[str]] = defaultdict(list)
    for path in files:
        groups[union.find(path)].append(path)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def stem_of(index: Index | None, path: str) -> str:
    """The family stem: the profile's (declarative `profile.stem` facet) when it differs,
    else the builtin one."""
    found = index.facet(path, "profile.stem") if index is not None else None
    if found is not None and isinstance(found[0], dict) and found[0].get("stem"):
        return str(found[0]["stem"])
    return name_stem(path)[0]


def family_key(paths: list[str], index: Index | None = None) -> str:
    stems = sorted({s for p in paths if (s := stem_of(index, p))})
    kinds = sorted({kind_of(p) for p in paths})
    return f"{'+'.join(kinds)}:{stems[0] if stems else PurePosixPath(paths[0]).name}"


def family_id(key: str, paths: list[str]) -> str:
    """Stable across scans while the family's kind and stem stay; the smallest member breaks
    ties between unrelated families that share a key."""
    return "fam_" + hashlib.sha256(f"{key}\0{min(paths)}".encode()).hexdigest()[:16]


# ----- ranking -------------------------------------------------------------------------------


def _version_tuple(text: Any) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(text or "")))


def _release(text: Any) -> tuple[int, int]:
    found = re.match(r"R(\d{4})([ab])", str(text or ""), re.IGNORECASE)
    return (int(found.group(1)), 0 if found.group(2).lower() == "a" else 1) if found else (0, 0)


def _active(path: str, index: Index) -> bool:
    row = index.rows.get(path)
    zone = row.zone if row is not None else None
    return not _negative(path) and zone not in ("backup_copy", "archive")


def _negative(path: str) -> bool:
    disk = disk_path(path)
    # Folder names count by their words: "old_scripts" and "Backup 2019" are old or backup
    # folders too (review cp17-234-a F5).
    folders = {w for p in disk.split("/")[:-1] for w in re.split(r"[\s_.\-]+", p.lower()) if w}
    tokens = set(name_stem(path)[1])
    return bool(folders & NEGATIVE_FOLDERS or tokens & NEGATIVE_TOKENS or _TOOL_SUFFIX.search(disk))


def _version_number(path: str) -> int | None:
    for token in name_stem(path)[1]:
        found = re.fullmatch(r"v(\d+)[a-z]?|rev(\d+)[a-z]?", token)
        if found:
            return int(found.group(1) or found.group(2))
    return None


@dataclass
class Candidate:
    """One candidate: a file and its copies (equal or normalised content)."""

    rep: str
    copies: list[str]
    reasons: list[dict[str, Any]] = field(default_factory=list)
    excluded: bool = False
    structural: bool = False  # excluded by a tool convention (T1/T2), not only by a statement

    @property
    def paths(self) -> list[str]:
        return [self.rep, *self.copies]


def _reason(
    rule: str, tier: str, text: str, evidence: Iterable[AdoptionEvidence]
) -> dict[str, Any]:
    ids = sorted({e.evidence_id for e in evidence})[:50]
    return {"rule": rule, "tier": tier, "text": text[:300], "evidence": ids}


def _classes(paths: list[str], index: Index) -> list[Candidate]:
    union = _Union()
    for p in paths:
        union.find(p)
    inside = set(paths)
    for p in paths:
        for e in index.related(p, "copy_of"):
            level = (
                (e.object.literal or {}).get("level")
                if isinstance(e.object.literal, dict)
                else None
            )
            if e.object.path in inside and level in ("exact", "normalised"):
                union.join(p, e.object.path)
    groups: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        groups[union.find(p)].append(p)
    out = []
    for group in groups.values():
        ordered = sorted(group, key=lambda p: (_negative(p), p.count("/"), len(p), p))
        out.append(Candidate(ordered[0], ordered[1:]))
    return sorted(out, key=lambda c: c.rep)


@dataclass
class Ranked:
    current: str | None
    band: str
    rule: str | None
    candidates: list[Candidate]
    conflicts: list[dict[str, Any]]


def _latest_statements(
    index: Index, paths: list[str], *, proposed: bool = False
) -> list[AdoptionEvidence]:
    """Written statements about ``paths``. Statements an AI task proposed (quote-verified, but
    read by a model) are left out unless ``proposed``: they may raise conflicts and never
    exclude, decide or corroborate (C17-AI, decision 0042)."""
    found: list[AdoptionEvidence] = []
    for p in paths:
        found += [e for e in index.statements_about(p) if proposed or e.determinism != "proposed"]
    return found


def _status_of(e: AdoptionEvidence) -> str:
    return (
        str((e.object.literal or {}).get("status", ""))
        if isinstance(e.object.literal, dict)
        else ""
    )


def _date_of(e: AdoptionEvidence) -> str:
    return (
        str((e.object.literal or {}).get("date") or "")
        if isinstance(e.object.literal, dict)
        else ""
    )


def rank(
    paths: list[str],
    index: Index,
    *,
    current_elsewhere: dict[str, str],
    decision: str | None = None,
) -> Ranked:
    """Rank one family. ``current_elsewhere``: files that are current members of other
    families or belong to none (for R3b and R4)."""
    classes = _classes(paths, index)
    conflicts: list[dict[str, Any]] = []
    inside = set(paths)

    # R2 exclusions: tool conventions and later statements of supersession
    for c in classes:
        for p in c.paths:
            for e in index.related(p, "autosave_of") + index.related(p, "backup_of"):
                kind = "an autosave" if e.predicate == "autosave_of" else "a release-upgrade backup"
                c.excluded = True
                c.structural = True
                c.reasons.append(_reason("R2", e.tier, f"{kind} of {e.object.path}", [e]))
        statements = _latest_statements(index, c.paths)
        if statements:
            latest_date = max(_date_of(e) for e in statements)
            latest = [e for e in statements if _date_of(e) == latest_date]
            verdicts = {_status_of(e) for e in latest}
            if len(verdicts) > 1:
                conflicts.append(
                    {
                        "kind": "statement_conflict",
                        "text": f"statements of the same date disagree about {c.rep}: "
                        + ", ".join(sorted(verdicts)),
                        "evidence": sorted(e.evidence_id for e in latest),
                        "paths": [c.rep],
                    }
                )
            elif verdicts & STATUS_EXCLUDES:
                c.excluded = True
                c.reasons.append(
                    _reason(
                        "R2",
                        "T4",
                        f"{latest[0].subject} says "
                        f"{latest[0].object.path} is {next(iter(verdicts))}"
                        f" ({latest_date or 'undated'})",
                        latest,
                    )
                )
            older = [
                e
                for e in statements
                if _date_of(e) != latest_date and _status_of(e) not in verdicts
            ]
            if older and len(verdicts) == 1:
                conflicts.append(
                    {
                        "kind": "contradicted_statement",
                        "text": f"an earlier statement about {c.rep} said "
                        + ", ".join(sorted({_status_of(e) for e in older}))
                        + f"; the later one ({latest_date}) says {next(iter(verdicts))}",
                        "evidence": sorted(e.evidence_id for e in older + latest),
                        "paths": [c.rep],
                    }
                )
    # A member excluded only by a written statement while current code uses it: the statement
    # and the use disagree, so the owner looks (review cp17-234-a r2 R2-1).
    for c in classes:
        if not c.excluded or c.structural:
            continue
        in_use = [
            e
            for p in c.paths
            for e in index.incoming.get(p, [])
            if e.predicate in ("calls", "model_reference", "used")
            and e.determinism == "pure"
            and e.tier in ("T1", "T2", "T3")
            and e.subject in current_elsewhere
            and e.subject not in inside
        ]
        if in_use:
            conflicts.append(
                {
                    "kind": "contradicted_statement",
                    "text": f"a statement excludes {c.rep}, but current code uses it: "
                    + ", ".join(sorted({e.subject for e in in_use})[:5]),
                    "evidence": sorted({e.evidence_id for e in in_use})[:100],
                    "paths": [c.rep],
                }
            )
    live = [c for c in classes if not c.excluded]

    def decide(winner: Candidate, rule: str, band: str) -> Ranked:
        _contradictions(winner, live, index, conflicts)
        _agreeing(winner, rule, live, index)
        # "Decided" means no kept contradiction (R4 section 3.3; review cp17-234-a F20); only
        # the owner's decision stays decided whatever the analyzers say.
        if (
            band == "decided"
            and rule != "R1"
            and any(
                c["kind"] in ("statement_conflict", "contradicted_statement", "old_member_in_use")
                for c in conflicts
            )
        ):
            band = "supported"
        return Ranked(winner.rep, band, rule, classes, conflicts)

    # R1 the owner's decision (T0) outranks everything, exclusions included: a member the
    # analyzers excluded stays current when the owner chose it, and the exclusion is kept as a
    # conflict for the owner to see.
    if decision is not None:
        chosen = next((c for c in classes if decision in c.paths), None)
        if chosen is not None:
            if chosen.excluded:
                chosen.excluded = False
                conflicts.append(
                    {
                        "kind": "owner_overrode_exclusion",
                        "text": f"the owner chose {chosen.rep}, which the analyzers excluded",
                        "evidence": sorted(
                            {i for r in chosen.reasons if r["rule"] == "R2" for i in r["evidence"]}
                        )[:100],
                        "paths": [chosen.rep],
                    }
                )
            chosen.reasons.append(_reason("R1", "T0", "the owner chose it", []))
            return decide(chosen, "R1", "decided")
    if not live:
        return Ranked(None, "undecided", None, classes, conflicts)
    if len(live) == 1:
        live[0].reasons.append(_reason("R2", "T2", "every other member is excluded", []))
        # Statements alone (T4) never make a family decided: R4 section 3.3 has T4 propose a
        # status that only the owner applies (review cp17-234-a F1).
        structural = any(c.structural for c in classes)
        band = (
            "decided"
            if structural
            else ("supported" if any(c.excluded for c in classes) else "weak")
        )
        return decide(live[0], "R2", band)

    # R3 identity order inside one origin (package uuid; revision, release)
    origin: dict[str, list[tuple[Candidate, tuple[Any, ...], list[AdoptionEvidence]]]] = (
        defaultdict(list)
    )
    for c in live:
        uuid = index.facet(c.rep, "package.uuid")
        if uuid is None:
            continue
        rev = index.facet(c.rep, "package.revision")
        rel = index.facet(c.rep, "package.release")
        key = (_version_tuple(rev[0] if rev else None), _release(rel[0] if rel else None))
        used = [x[1] for x in (uuid, rev, rel) if x is not None]
        origin[str(uuid[0])].append((c, key, used))
    for members in origin.values():
        if len(members) == len(live) and len(members) > 1:
            ordered = sorted(members, key=lambda m: m[1], reverse=True)
            if ordered[0][1] > ordered[1][1]:
                winner, key, used = ordered[0]
                winner.reasons.append(
                    _reason(
                        "R3",
                        "T1",
                        f"same model identity; highest revision and release "
                        f"({'.'.join(map(str, key[0])) or '?'}, R{key[1][0]}{'ab'[key[1][1]]})",
                        used,
                    )
                )
                return decide(winner, "R3", "decided")

    # R3b produced by the current version of its producer (T6 literal)
    produced = {}
    for c in live:
        hits = [
            e
            for p in c.paths
            for e in index.related(p, "generated_by")
            if e.object.path in current_elsewhere
            and e.object.path not in inside
            and e.determinism == "pure"  # an agent's report never decides (review F4)
        ]
        if hits:
            produced[c.rep] = hits
    # R4 used by a current file outside the family (T3 from PrometheOS's own reading decides;
    # literal references and agents' reports only appear as agreeing reasons)
    used3 = {}
    for c in live:
        incoming = [
            e
            for p in c.paths
            for e in index.incoming.get(p, [])
            if e.predicate in ("calls", "model_reference", "used", "references", "cache_of")
            and e.subject in current_elsewhere
            and e.subject not in inside
            and e.predicate != "cache_of"
        ]
        # Only PrometheOS's own reading decides: an agent-reported dependency (determinism
        # "reported") or an AI proposal corroborates at most (review cp17-234-a F4).
        strong = [e for e in incoming if e.tier in ("T1", "T2", "T3") and e.determinism == "pure"]
        if strong:
            used3[c.rep] = strong
    if len(used3) == 1:
        rep = next(iter(used3))
        winner = next(c for c in live if c.rep == rep)
        users = sorted({e.subject for e in used3[rep]})
        winner.reasons.append(
            _reason(
                "R4",
                "T3",
                "used by current files outside the family: " + ", ".join(users[:5]),
                used3[rep],
            )
        )
        if _negative(winner.rep) and any(not _negative(c.rep) for c in live if c is not winner):
            conflicts.append(
                {
                    "kind": "old_member_in_use",
                    "text": f"the member in use ({winner.rep}) is named or filed as old or a "
                    "backup, and another member is not",
                    "evidence": sorted({e.evidence_id for e in used3[rep]})[:100],
                    "paths": sorted(c.rep for c in live)[:20],
                }
            )
        strong_users = any(current_elsewhere.get(u) == "decided" for u in users)
        return decide(winner, "R4", "decided" if strong_users else "supported")
    if len(produced) == 1:
        rep = next(iter(produced))
        winner = next(c for c in live if c.rep == rep)
        producers = sorted({e.object.path or "" for e in produced[rep]})
        winner.reasons.append(
            _reason(
                "R3b",
                "T6",
                "written by the current version of its producer: " + ", ".join(producers[:5]),
                produced[rep],
            )
        )
        corroborated = _corroborators(winner, live, index)
        return decide(winner, "R3b", "supported" if corroborated else "weak")

    # R5 the latest written statement that a member is current
    claims = {}
    for c in live:
        statements = _latest_statements(index, c.paths)
        if statements:
            latest_date = max(_date_of(e) for e in statements)
            latest = [e for e in statements if _date_of(e) == latest_date]
            if {_status_of(e) for e in latest} <= STATUS_CURRENT:
                claims[c.rep] = (latest_date, latest)
    if claims:
        best = max(claims.values(), key=lambda v: v[0])[0]
        top = [rep for rep, v in claims.items() if v[0] == best]
        if len(top) == 1:
            winner = next(c for c in live if c.rep == top[0])
            winner.reasons.append(
                _reason(
                    "R5",
                    "T4",
                    f"the latest statement ({best or 'undated'}) calls it current",
                    claims[top[0]][1],
                )
            )
            return decide(winner, "R5", "supported")
        conflicts.append(
            {
                "kind": "statement_conflict",
                "text": "statements of the same date name different current members",
                "evidence": sorted(e.evidence_id for r in top for e in claims[r][1]),
                "paths": sorted(top),
            }
        )

    # R7 embedded time (T1, never decisive alone)
    times = _embedded_times(live, index)
    if len(times) == len(live) and len(live) > 1:
        newest = sorted(times.items(), key=lambda kv: kv[1][0], reverse=True)
        if newest[0][1][0] > newest[1][1][0]:
            rep, (when, ev, name) = newest[0]
            winner = next(c for c in live if c.rep == rep)
            winner.reasons.append(
                _reason("R7", "T1", f"newest embedded time ({name} {when})", [ev])
            )
            naming = _naming_choice(live)
            band = "supported" if naming is winner else "weak"
            if naming is winner:
                winner.reasons.append(_reason("R8", "T7", "highest version number in its name", []))
            return decide(winner, "R7", band)

    # R8 version numbers and negative names (T7)
    naming = _naming_choice(live)
    if naming is not None:
        naming.reasons.append(
            _reason(
                "R8",
                "T7",
                "the only member without an old/backup/copy "
                "mark, or the highest version number in its name",
                [],
            )
        )
        return decide(naming, "R8", "weak")
    return Ranked(None, "undecided", None, classes, conflicts)


def _naming_choice(live: list[Candidate]) -> Candidate | None:
    numbered = [(c, _version_number(c.rep)) for c in live]
    if all(n is not None for _c, n in numbered):
        ordered = sorted(numbered, key=lambda x: x[1] or 0, reverse=True)
        if (ordered[0][1] or 0) > (ordered[1][1] or 0):
            return ordered[0][0]
    clean = [c for c in live if not _negative(c.rep)]
    if len(clean) == 1:
        return clean[0]
    return None


def _embedded_times(
    live: list[Candidate], index: Index
) -> dict[str, tuple[str, AdoptionEvidence, str]]:
    """Each member's embedded time, when every member has one on the same clock. A MAT header
    holds the writing machine's local time, so MAT times compare only within one platform;
    package times are UTC (review cp17-234-a F13)."""
    times: dict[str, tuple[str, AdoptionEvidence, str]] = {}
    clocks: set[str] = set()
    for c in live:
        for name in ("mat.created", "package.modified", "package.created"):
            hit = index.facet(c.rep, name)
            if hit is not None and hit[0]:
                times[c.rep] = (str(hit[0]), hit[1], name)
                platform = index.facet(c.rep, "mat.platform")
                clocks.add(
                    f"mat:{platform[0] if platform else '?'}" if name == "mat.created" else "utc"
                )
                break
    return times if len(clocks) == 1 else {}


def _corroborators(winner: Candidate, live: list[Candidate], index: Index) -> bool:
    """Whether an independent rule agrees: the newest embedded time, a current statement, or
    the version number in the name."""
    times = {rep: when for rep, (when, _ev, _name) in _embedded_times(live, index).items()}
    if (
        times
        and max(times, key=lambda k: times[k]) == winner.rep
        and len(set(times.values())) == len(times)
    ):
        return True
    statements = _latest_statements(index, winner.paths)
    if statements and {_status_of(e) for e in statements} & STATUS_CURRENT:
        return True
    return _naming_choice(live) is winner


def _agreeing(winner: Candidate, rule: str, live: list[Candidate], index: Index) -> None:
    """Independent rules that agree with the deciding one are shown as supporting reasons."""
    if rule != "R5":
        statements = _latest_statements(index, winner.paths)
        if statements:
            latest_date = max(_date_of(e) for e in statements)
            latest = [e for e in statements if _date_of(e) == latest_date]
            if {_status_of(e) for e in latest} <= STATUS_CURRENT:
                winner.reasons.append(
                    _reason(
                        "R5",
                        "T4",
                        f"agrees: the latest statement "
                        f"({latest_date or 'undated'}) calls it current",
                        latest,
                    )
                )
    if rule != "R7" and len(live) > 1:
        times = {rep: (when, ev) for rep, (when, ev, _name) in _embedded_times(live, index).items()}
        if len(times) == len(live):
            newest = sorted(times.items(), key=lambda kv: kv[1][0], reverse=True)
            if newest[0][0] == winner.rep and newest[0][1][0] > newest[1][1][0]:
                winner.reasons.append(
                    _reason(
                        "R7",
                        "T1",
                        f"agrees: newest embedded time ({newest[0][1][0]})",
                        [newest[0][1][1]],
                    )
                )
    reported = [
        e
        for p in winner.paths
        for e in index.incoming.get(p, [])
        if e.predicate == "used" and e.determinism == "reported"
    ]
    if reported:
        winner.reasons.append(
            _reason(
                "R4",
                "T3",
                "agrees: an agent's tool reported it is used by "
                + ", ".join(sorted({e.subject for e in reported})[:5]),
                reported,
            )
        )


def _contradictions(
    winner: Candidate, live: list[Candidate], index: Index, conflicts: list[dict[str, Any]]
) -> None:
    """Statements that call another live member current or the winner superseded are kept."""
    for c in live:
        statements = _latest_statements(index, c.paths, proposed=True)
        if c is winner:
            against = [e for e in statements if _status_of(e) in STATUS_EXCLUDES]
            text = f"a statement calls the chosen member {winner.rep} superseded or invalid"
        else:
            against = [e for e in statements if _status_of(e) in STATUS_CURRENT]
            text = f"a statement calls {c.rep} current or final, not {winner.rep}"
        if against:
            conflicts.append(
                {
                    "kind": "contradicted_statement",
                    "text": text[:300],
                    "evidence": sorted(e.evidence_id for e in against),
                    "paths": sorted({winner.rep, c.rep}),
                }
            )


# ----- the whole generation -----------------------------------------------------------------


def _decision(fid: str, paths: list[str], decisions: dict[str, str]) -> str | None:
    """The owner's choice for a family: the newest decision whose file is one of its members.
    The family ID is not trusted for this: it changes when the smallest member moves and can
    come back to an older value, which must not revive an older decision (review
    cp17-5-a-r3 R3-1). ``decisions`` runs oldest to newest (``plan.decisions_for``), each one
    already found by path or digest in this generation."""
    del fid  # kept in the signature for the caller's readability
    members = set(paths)
    return next((p for p in reversed(list(decisions.values())) if p in members), None)


def fuse(
    scan_gen: int,
    rows: list[AdoptionFile],
    evidence: list[AdoptionEvidence],
    decisions: dict[str, str] | None = None,
) -> list[AdoptionFamily]:
    """Families of one generation with ranked members. Two passes: the second sees which
    producers and users are current (R3b, R4)."""
    index = Index({r.path: r for r in rows}, evidence)
    groups = families(index)
    grouped = {p for g in groups for p in g}
    singles = set(candidates(index.rows)) - grouped
    decisions = decisions or {}
    # A user or producer counts as current only when it is live: not in a backup, old or archive
    # folder or zone and not named old/backup/copy (review cp17-234-a F5). Its band travels with
    # it, so a weakly ranked user cannot decide another family.
    base = {p: "decided" for p in singles if _active(p, index)}
    current: dict[str, str] = dict(base)
    ranked: dict[str, Ranked] = {}
    for _ in range(2):
        nxt: dict[str, str] = dict(base)
        for paths in groups:
            fid = family_id(family_key(paths, index), paths)
            result = rank(
                paths, index, current_elsewhere=current, decision=_decision(fid, paths, decisions)
            )
            ranked[fid] = result
            if result.current is not None:
                winner = next(c for c in result.candidates if c.rep == result.current)
                for p in winner.paths:
                    if p == result.current or _active(p, index):
                        nxt[p] = result.band
        current = nxt
    out = []
    for paths in groups:
        key = family_key(paths, index)
        fid = family_id(key, paths)
        result = ranked[fid]
        members = []
        order = _order(result)
        for c in result.candidates:
            outcome = (
                "current"
                if c.rep == result.current
                else ("excluded" if c.excluded else "candidate")
            )
            members.append(
                {
                    "path": c.rep,
                    "outcome": outcome,
                    "copy_of": None,
                    "rank": order.get(c.rep),
                    "reasons": c.reasons,
                }
            )
            for copy in c.copies:
                members.append(
                    {"path": copy, "outcome": "copy", "copy_of": c.rep, "rank": None, "reasons": []}
                )
        out.append(
            AdoptionFamily.model_validate(
                {
                    "scan_gen": scan_gen,
                    "family_id": fid,
                    "key": key[:200],
                    "kind": sorted({kind_of(p) for p in paths})[0]
                    if len({kind_of(p) for p in paths}) == 1
                    else "mixed",
                    "members": sorted(
                        members, key=lambda m: (m["rank"] is None, m["rank"] or 0, m["path"])
                    ),
                    "current": result.current,
                    "band": result.band,
                    "deciding_rule": result.rule,
                    "conflicts": result.conflicts,
                }
            )
        )
    return sorted(out, key=lambda f: f.family_id)


def _order(result: Ranked) -> dict[str, int]:
    """Ranks: 1 for the current member, then live candidates by path, excluded ones last."""
    order: dict[str, int] = {}
    position = 1
    if result.current is not None:
        order[result.current] = position
        position += 1
    for c in sorted(result.candidates, key=lambda c: (c.excluded, c.rep)):
        if c.rep not in order:
            order[c.rep] = position
            position += 1
    return order
