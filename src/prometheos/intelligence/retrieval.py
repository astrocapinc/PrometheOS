"""Retrieval for linking (A07.8, CP22.5; decision 0095).

Proposing which question a claim answers, or which records relate, needs candidates. Keyword
search (BM25 over the store's FTS5 index) finds shared words; a local embedding finds shared
meaning. The two rankings are fused by reciprocal rank (RRF). Results are record ids only, for
linking and proposing: there is no question-answering engine.

- **Measured first.** A retrieval record fixes the labelled queries, ``k``, the recall target, the
  minimum number of queries and the comparison (one-sided 95% Clopper-Pearson on the queries where
  the two methods differ) before any measurement. The fusion is enabled for an embedder only when
  its recall@k meets the target and it beats BM25 alone: more queries improve than worsen, with the
  lower bound of that share above one half. Otherwise retrieval stays BM25.
- **Local only.** The embedder runs on this computer. Tests and walks use the fixture embedder
  (token hashing, deterministic, no download); a real model is a pre-installed file whose digest
  the receipt records, installed only with the owner's OK.
- **Visibility.** Candidates are the records the caller may see; restricted and private records
  never enter an agent's results.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from typing import Annotated, Any, Protocol

from pydantic import Field, StringConstraints, model_validator

from prometheos.app.reads import Caller
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, StrictModel, utc_now
from prometheos.store import db
from prometheos.store.derived import search_mode
from prometheos.store.project import ProjectStore

from .evaluation import clopper_pearson_lower

RRF_K = 60
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class Embedder(Protocol):
    name: str
    digest: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """The fixture embedder: lower-cased word tokens and their character trigrams hashed into a
    fixed-size vector, L2-normalised. Deterministic and local; it shares no weights with any
    model and confers no claim about real embedding quality."""

    name = "fixture-hashing-v1"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.digest = hashlib.sha256(f"{self.name}:{dim}".encode()).hexdigest()

    def _features(self, text: str) -> list[str]:
        words = [w.lower() for w in _TOKEN.findall(text)]
        grams = [w[i : i + 3] for w in words for i in range(max(1, len(w) - 2))]
        return words + grams

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dim
            for feature in self._features(text):
                h = int.from_bytes(hashlib.sha256(feature.encode()).digest()[:8], "big")
                vector[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out


def _fts_query(text: str) -> str:
    words = sorted({w.lower() for w in _TOKEN.findall(text) if len(w) > 2})
    return " OR ".join(f'"{w}"' for w in words[:64]) or '""'


def candidates(conn: sqlite3.Connection, caller: Caller, kinds: tuple[str, ...]) -> dict[str, str]:
    """record id -> searchable text (title and text) of every visible, live record of the kinds."""
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"SELECT s.record_id, s.title, s.text, r.sensitivity, r.lifecycle FROM search_text s "  # noqa: S608
        f"JOIN records r ON r.id = s.record_id WHERE s.kind IN ({marks})",
        kinds,
    ).fetchall()
    return {r[0]: f"{r[1]}\n{r[2]}" for r in rows if r[4] != "deleted" and caller.may_see(r[3])}


def bm25_rank(
    conn: sqlite3.Connection, query: str, allowed: set[str], k: int,
    texts: dict[str, str] | None = None,
) -> list[str]:  # fmt: skip
    """Keyword ranking over exactly the allowed records: FTS5's BM25 where SQLite has it (the
    candidates are filtered inside the query, so records the caller may not see never take a
    place in the ranking; review cp22-a R2-F4), else the count of query words a record holds."""
    words = {w.lower() for w in _TOKEN.findall(query) if len(w) > 2}
    if not allowed or not words:
        return []
    if search_mode(conn) == "fts5":
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS retrieval_allowed (record_id TEXT PRIMARY KEY)"
        )
        conn.execute("DELETE FROM retrieval_allowed")
        conn.executemany("INSERT INTO retrieval_allowed VALUES (?)", [(r,) for r in allowed])
        rows = conn.execute(
            "SELECT s.record_id FROM search_fts f JOIN search_text s ON s.rowid = f.rowid "
            "JOIN retrieval_allowed a ON a.record_id = s.record_id "
            "WHERE search_fts MATCH ? ORDER BY bm25(search_fts) LIMIT ?",
            (_fts_query(query), k),
        ).fetchall()
        return [r[0] for r in rows]
    have = texts or {}
    hits = {rid: sum(1 for w in words if w in have.get(rid, "").lower()) for rid in allowed}
    return sorted((rid for rid in hits if hits[rid]), key=lambda rid: (-hits[rid], rid))[:k]


def embed_rank(embedder: Embedder, query: str, texts: dict[str, str], k: int) -> list[str]:
    if not texts:
        return []
    ids = sorted(texts)
    vectors = embedder.embed([query] + [texts[i] for i in ids])
    q = vectors[0]
    scores = {
        rid: sum(a * b for a, b in zip(q, v, strict=True))
        for rid, v in zip(ids, vectors[1:], strict=True)
    }
    return sorted(ids, key=lambda rid: (-scores[rid], rid))[:k]


def rrf(rankings: list[list[str]], k: int) -> list[str]:
    score: dict[str, float] = {}
    for ranking in rankings:
        for position, rid in enumerate(ranking):
            score[rid] = score.get(rid, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(score, key=lambda rid: (-score[rid], rid))[:k]


def rank(conn: sqlite3.Connection, caller: Caller, query: str, *, kinds: tuple[str, ...], k: int,
         embedder: Embedder | None) -> dict[str, Any]:  # fmt: skip
    texts = candidates(conn, caller, kinds)
    lexical = bm25_rank(conn, query, set(texts), k, texts)
    if embedder is None:
        return {"ids": lexical, "method": "bm25"}
    semantic = embed_rank(embedder, query, texts, k)
    return {"ids": rrf([lexical, semantic], k), "method": "hybrid", "embedder": embedder.name}


# ----- measurement fixed in advance ---------------------------------------------------------------


class LabelledQuery(StrictModel):
    query_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,80}$")]
    text: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    relevant: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=50
    )


class RetrievalRecord(StrictModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,80}$")]
    kinds: list[Annotated[str, StringConstraints(pattern=r"^[a-z_]+$")]] = Field(min_length=1)
    queries: list[LabelledQuery] = Field(min_length=1, max_length=2000)
    k: int = Field(ge=1, le=100)
    recall_target: float = Field(gt=0, le=1)
    min_queries: int = Field(ge=1)
    confidence: float
    method: str

    @model_validator(mode="after")
    def _fixed(self) -> RetrievalRecord:
        if self.confidence != 0.95 or self.method != "paired_clopper_pearson_one_sided":
            raise ValueError("the comparison is fixed: 95%, one-sided, paired Clopper-Pearson")
        if self.min_queries > len(self.queries):
            raise ValueError("fewer labelled queries than the minimum")
        return self


def fix(store: ProjectStore, record: RetrievalRecord, *, by: str) -> str:
    """Store the retrieval record by digest before any measurement; a later record under the
    same name must be the same record (review cp22-a R2-F5)."""
    digest = canonical_sha256(record.model_dump(mode="json"))
    queries = canonical_sha256([q.model_dump(mode="json") for q in record.queries])
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            row = conn.execute(
                "SELECT record_sha256 FROM ai_retrieval_records WHERE name = ? OR "
                "queries_sha256 = ?", (record.name, queries),
            ).fetchone()  # fmt: skip
            if row is not None:
                # One record per name and per query set: a target, k or comparison is never
                # changed by fixing the same queries again (review cp22-a R3-F4).
                if row[0] != digest:
                    raise PrometheosError(
                        "validation_error",
                        "a retrieval record with this name or these queries is already fixed; "
                        "its queries, k, target and comparison never change",
                        {"name": record.name},
                    )
                return digest
            conn.execute(
                "INSERT INTO ai_retrieval_records (record_sha256, name, queries_sha256, record, "
                "fixed_by, fixed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (digest, record.name, queries,
                 json.dumps(record.model_dump(mode="json"), sort_keys=True), by, utc_now()),
            )  # fmt: skip
    finally:
        conn.close()
    return digest


def recall_at(ids: list[str], relevant: list[str], k: int) -> float:
    return len(set(ids[:k]) & set(relevant)) / len(set(relevant))


def measure(conn: sqlite3.Connection, caller: Caller, record: RetrievalRecord,
            embedder: Embedder) -> dict[str, Any]:  # fmt: skip
    digest = canonical_sha256(record.model_dump(mode="json"))
    if (
        conn.execute(
            "SELECT 1 FROM ai_retrieval_records WHERE record_sha256 = ?", (digest,)
        ).fetchone()
        is None
    ):
        raise PrometheosError(
            "validation_error", "fix the retrieval record before measuring", {"record": digest}
        )
    kinds = tuple(record.kinds)
    bm25: list[float] = []
    hybrid: list[float] = []
    for q in record.queries:
        bm25.append(recall_at(rank(conn, caller, q.text, kinds=kinds, k=record.k,
                                   embedder=None)["ids"], q.relevant, record.k))  # fmt: skip
        hybrid.append(recall_at(rank(conn, caller, q.text, kinds=kinds, k=record.k,
                                     embedder=embedder)["ids"], q.relevant, record.k))  # fmt: skip
    wins = sum(1 for a, b in zip(hybrid, bm25, strict=True) if a > b)
    losses = sum(1 for a, b in zip(hybrid, bm25, strict=True) if a < b)
    n = len(record.queries)
    hybrid_recall = sum(hybrid) / n
    win_bound = clopper_pearson_lower(wins, wins + losses, record.confidence)
    beats = wins + losses > 0 and win_bound > 0.5
    return {
        "record_sha256": canonical_sha256(record.model_dump(mode="json")),
        "embedder": embedder.name,
        "embedder_digest": embedder.digest,
        "queries": n,
        "k": record.k,
        "bm25_recall": round(sum(bm25) / n, 6),
        "hybrid_recall": round(hybrid_recall, 6),
        "wins": wins,
        "losses": losses,
        "ties": n - wins - losses,
        "win_share_lower_bound": round(win_bound, 6),
        "meets_target": hybrid_recall >= record.recall_target and n >= record.min_queries,
        "beats_bm25": beats,
        "enable_fusion": beats
        and hybrid_recall >= record.recall_target
        and n >= record.min_queries,
    }
