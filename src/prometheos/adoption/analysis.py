"""Running the analyzer pack over one scan generation (A05, CP17.2; decision 0040).

Extractor outputs are cached by ``(analyzer, version, params digest, input sha256)`` in
``adoption_cache``; linkers always run over the whole generation (they are cheap and their
answers depend on every file). The generation's evidence is replaced as a whole, so analysing
again gives the same rows (evidence IDs are digests of their content).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheos.adoption import analyzers as az
from prometheos.adoption import assist, tables
from prometheos.adoption.contracts import AdoptionEvidence, AdoptionFile, AdoptionProfile
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.models import utc_now


@dataclass
class Analysis:
    evidence: list[AdoptionEvidence]
    cache_new: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    extracted: int = 0
    replayed: int = 0
    unreadable: list[str] = field(default_factory=list)
    unavailable: dict[str, tuple[str, int]] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        by_predicate: dict[str, int] = {}
        for e in self.evidence:
            by_predicate[e.predicate] = by_predicate.get(e.predicate, 0) + 1
        return {
            "evidence": len(self.evidence),
            "by_predicate": dict(sorted(by_predicate.items())),
            "extracted": self.extracted,
            "replayed": self.replayed,
            "changed_since_inventory": len(self.unreadable),
            "analyzers_unavailable": [
                {"analyzer": a, "reason": why, "install": az.INSTALL_HINT[a], "files": files}
                for a, (why, files) in sorted(self.unavailable.items())
            ],
        }


def cached(conn: sqlite3.Connection, analyzer: str, sha256: str) -> dict[str, Any] | None:
    declaration = az.ANALYZERS[analyzer]
    row = conn.execute(
        "SELECT output FROM adoption_cache WHERE analyzer = ? AND analyzer_version = ? "
        "AND params_sha256 = ? AND input_sha256 = ?",
        (analyzer, declaration.version, az.params_digest(analyzer), sha256),
    ).fetchone()
    if row is None:
        return None
    value: dict[str, Any] = json.loads(row[0])
    return value


def analyze(
    conn: sqlite3.Connection,
    root: Path,
    scan_gen: int,
    rows: list[AdoptionFile],
    *,
    progress: Callable[[int, int], None] | None = None,
    profile: AdoptionProfile | None = None,
) -> Analysis:
    """Evidence for ``rows`` (one generation). ``conn`` is read for the cache only; the caller
    stores :attr:`Analysis.evidence` and :attr:`Analysis.cache_new`."""
    corpus = az.Corpus({r.path: r for r in rows}, profile=profile)
    result = Analysis(evidence=[])
    # An extractor this installation cannot run is skipped whole, cache included, and named in
    # the summary (decision 0053): its evidence is absent, never silently stale or partial.
    missing = az.unavailable()
    for analyzer, why in missing.items():
        files = sum(1 for r in rows if az.applies(analyzer, r))
        if files:  # an analyzer with nothing to read in this folder loses nothing here
            result.unavailable[analyzer] = (why, files)
    work = [(a, r) for a in az.EXTRACTORS if a not in missing for r in rows if az.applies(a, r)]
    for done, (analyzer, row) in enumerate(work, start=1):
        if progress is not None and done % 25 == 0:
            progress(done, len(work))
        assert row.sha256 is not None
        output = cached(conn, analyzer, row.sha256)
        if output is not None:
            result.replayed += 1
        else:
            output = az.extract(analyzer, root, row)
            if output is None:
                result.unreadable.append(row.path)
                continue
            result.extracted += 1
            declaration = az.ANALYZERS[analyzer]
            result.cache_new.append(
                (
                    analyzer,
                    declaration.version,
                    az.params_digest(analyzer),
                    row.sha256,
                    dumps_canonical(output).decode("utf-8"),
                )
            )
        corpus.outputs[(analyzer, row.path)] = output
    facts = az.link(corpus) + az.project_facts(corpus, root) + az.near_copies(corpus, root)
    facts += az.graphify_facts(corpus, root)
    for path in result.unreadable:
        facts.append(
            az.Fact(
                "identity.copies",
                path,
                "unresolved",
                {"literal": {"kind": "changed_since_inventory"}},
                "T1",
            )
        )
    replayed_ai: list[AdoptionEvidence] = []
    for accepted in assist.accepted_results(conn):
        subject_row = corpus.rows.get(accepted["subject"])
        current_template = assist.template_sha256(accepted["kind"])
        if subject_row is None or subject_row.sha256 != accepted["subject_sha256"]:
            continue  # the file changed: the task is asked again, never reused
        if accepted["template_sha256"] != current_template:
            continue
        if any(
            p not in corpus.rows for p in assist.paths_named(accepted["kind"], accepted["output"])
        ):
            continue  # a file it names is gone or moved: asked again (review cp17-234-a F22)
        replayed_ai += assist.evidence(
            scan_gen, accepted["kind"], accepted["subject"], accepted["subject_sha256"],
            accepted["output"],
        )  # fmt: skip
    seen: dict[str, AdoptionEvidence] = {e.evidence_id: e for e in replayed_ai}
    for fact in facts:
        subject = corpus.rows.get(fact.subject)
        evidence = fact.evidence(scan_gen, subject.sha256 if subject is not None else None)
        seen.setdefault(evidence.evidence_id, evidence)
    result.evidence = sorted(seen.values(), key=lambda e: (e.subject, e.predicate, e.evidence_id))
    return result


def store(conn: sqlite3.Connection, scan_gen: int, analysis: Analysis) -> None:
    """Replace the generation's evidence and add new cache entries (inside the caller's
    transaction)."""
    tables.replace_evidence(conn, scan_gen, analysis.evidence)
    now = utc_now()
    conn.executemany(
        "INSERT OR IGNORE INTO adoption_cache (analyzer, analyzer_version, params_sha256, "
        "input_sha256, output, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(*entry, now) for entry in analysis.cache_new],
    )
