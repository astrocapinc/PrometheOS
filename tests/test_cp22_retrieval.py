"""CP22.5 (A07; decision 0095): retrieval for linking (C22-RETRIEVAL, S72).

Nothing is downloaded and no model runs: the fixture embedder hashes tokens locally.
- results are record ids of records the caller may see; restricted records never reach an agent;
- BM25 over the store's FTS5 index, the embedding ranking and their reciprocal-rank fusion;
- the measurement record fixes the queries, k, the target and the paired comparison first;
- the fusion is enabled only when it meets the target and beats BM25 alone (the lower bound of
  the share of improved queries above one half); a tie or a loss keeps BM25.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import Env, create_entry, request

from prometheos.app.reads import Caller
from prometheos.domain.models import PrometheosError
from prometheos.intelligence import retrieval as rt
from prometheos.store.project import ProjectStore

AGENT = Caller("test-agent")


def test_rrf_rewards_agreement_and_breaks_ties_by_id() -> None:
    assert rt.rrf([["a", "b", "c"], ["b", "a", "d"]], 3)[:2] == ["a", "b"]
    assert rt.rrf([["x"], ["y"]], 2) == ["x", "y"]


def test_the_fixture_embedder_is_deterministic_and_normalised() -> None:
    e = rt.HashingEmbedder(64)
    one, two = e.embed(["flywheel loss at speed", "flywheel loss at speed"])
    assert one == two and abs(sum(v * v for v in one) - 1) < 1e-9
    close, far = e.embed(["flywheel losses at speeds", "invoice for the cafeteria"])
    score = lambda v: sum(a * b for a, b in zip(one, v, strict=True))  # noqa: E731
    assert score(close) > score(far)


def record(**change: Any) -> rt.RetrievalRecord:
    base = {"name": "claims-to-questions", "kinds": ["entry"],
            "queries": [{"query_id": "q1", "text": "x", "relevant": ["r1"]}], "k": 5,
            "recall_target": 0.5, "min_queries": 1, "confidence": 0.95,
            "method": "paired_clopper_pearson_one_sided"}  # fmt: skip
    return rt.RetrievalRecord.model_validate(base | change)


def test_the_comparison_is_fixed() -> None:
    with pytest.raises(ValueError):
        record(confidence=0.9)
    with pytest.raises(ValueError):
        record(method="point_estimate")
    with pytest.raises(ValueError):
        record(min_queries=5)


NOTES = [
    (
        "Flywheel losses",
        "Bearing friction dominates the flywheel losses above 20000 rpm.",
        "normal",
    ),
    (
        "Battery ageing",
        "Calendar ageing of the pack at 35 C halves capacity in six years.",
        "normal",
    ),
    (
        "Regenerative braking",
        "Braking energy recovered by the flywheel during urban cycles.",
        "normal",
    ),
    ("Supervisor feedback", "Private remarks about the flywheel schedule.", "restricted"),
]


@pytest.fixture
def world(tmp_path: Path) -> tuple[Env, dict[str, str]]:
    env = Env(tmp_path)
    ids: dict[str, str] = {}
    ops = []
    for title, text, sensitivity in NOTES:
        op = create_entry(sensitivity=sensitivity, title=title, text=text)
        ids[title] = op["id"]
        ops.append(op)
    seq = env.svc.project_info(env.project_id, AGENT)["project_seq"]
    done = env.svc.apply(
        env.project_id, request(env.project_id, ops, seq), Caller("o", scope="owner")
    )
    assert done["ok"], done
    return env, ids


def ranked(env: Env, query: str, embedder: Any, caller: Caller = AGENT) -> dict[str, Any]:
    conn = ProjectStore(env.store_dir).connect()
    try:
        return rt.rank(conn, caller, query, kinds=("entry",), k=3, embedder=embedder)
    finally:
        conn.close()


def test_results_are_visible_record_ids_only(world: tuple[Env, dict[str, str]]) -> None:
    env, ids = world
    lexical = ranked(env, "flywheel losses", None)
    assert lexical["method"] == "bm25" and lexical["ids"][0] == ids["Flywheel losses"]
    assert ids["Supervisor feedback"] not in lexical["ids"]
    hybrid = ranked(env, "flywheel losses", rt.HashingEmbedder())
    assert hybrid["method"] == "hybrid" and set(hybrid["ids"]) <= set(ids.values())
    assert ids["Supervisor feedback"] not in hybrid["ids"]
    owner = ranked(env, "flywheel schedule remarks", None, Caller("o", scope="owner"))
    assert ids["Supervisor feedback"] in owner["ids"]


def test_fusion_is_enabled_only_when_it_beats_bm25(world: tuple[Env, dict[str, str]]) -> None:
    env, ids = world
    # A wording BM25 cannot match (no shared word) but the trigram embedding can.
    queries = [
        {
            "query_id": f"q{i}",
            "text": "flywheels lossy bearings",
            "relevant": [ids["Flywheel losses"]],
        }
        for i in range(3)
    ]
    first = record(name="three-wins", queries=queries, k=1, recall_target=0.5, min_queries=3)
    tie = record(name="a-tie", queries=[{"query_id": "q1", "text": "Battery ageing",
                                         "relevant": [ids["Battery ageing"]]}], k=1)  # fmt: skip
    for fixed in (first, tie):
        rt.fix(ProjectStore(env.store_dir), fixed, by="test")
    conn = ProjectStore(env.store_dir).connect()
    try:
        measured = rt.measure(conn, AGENT, first, rt.HashingEmbedder())
        same = rt.measure(conn, AGENT, tie, rt.HashingEmbedder())
    finally:
        conn.close()
    assert measured["bm25_recall"] < measured["hybrid_recall"]
    # three wins out of three: the one-sided lower bound 0.05^(1/3) = 0.37 is not above one half
    assert measured["wins"] == 3 and measured["beats_bm25"] is False
    assert measured["enable_fusion"] is False
    assert same["ties"] == 1 and same["enable_fusion"] is False


def test_five_wins_and_no_loss_enable_the_fusion(world: tuple[Env, dict[str, str]]) -> None:
    env, ids = world
    queries = [
        {"query_id": f"q{i}", "text": "flywheels lossy bearings",
         "relevant": [ids["Flywheel losses"]]}
        for i in range(5)
    ]  # fmt: skip
    lenient = record(name="five", queries=queries, k=1, recall_target=0.5, min_queries=5)
    others = [{**q, "query_id": f"s{i}"} for i, q in enumerate(queries)]  # its own query set
    strict = record(name="five-strict", queries=others, k=1, recall_target=1.0, min_queries=5)
    for fixed in (lenient, strict):
        rt.fix(ProjectStore(env.store_dir), fixed, by="test")
    conn = ProjectStore(env.store_dir).connect()
    try:
        measured = rt.measure(conn, AGENT, lenient, rt.HashingEmbedder())
        stricter = rt.measure(conn, AGENT, strict, rt.HashingEmbedder())
    finally:
        conn.close()
    # 0.05 ** (1 / 5) = 0.549 > 0.5: the fusion beats BM25 and meets the target
    assert measured["wins"] == 5 and measured["losses"] == 0
    assert measured["beats_bm25"] and measured["enable_fusion"] is True
    assert stricter["hybrid_recall"] == 1.0 and stricter["enable_fusion"] is True


def test_r2_f5_the_record_is_fixed_first_and_never_changed(
    world: tuple[Env, dict[str, str]],
) -> None:
    env, ids = world
    rec = record(name="fixed", queries=[{"query_id": "q1", "text": "flywheel",
                                         "relevant": [ids["Flywheel losses"]]}])  # fmt: skip
    conn = ProjectStore(env.store_dir).connect()
    try:
        with pytest.raises(PrometheosError, match="fix the retrieval record"):
            rt.measure(conn, AGENT, rec, rt.HashingEmbedder())
    finally:
        conn.close()
    rt.fix(ProjectStore(env.store_dir), rec, by="test")
    assert rt.fix(ProjectStore(env.store_dir), rec, by="test")  # the same record: idempotent
    with pytest.raises(PrometheosError, match="never change"):
        changed = record(
            name="fixed",
            k=2,
            queries=[{"query_id": "q1", "text": "flywheel", "relevant": [ids["Flywheel losses"]]}],
        )
        rt.fix(ProjectStore(env.store_dir), changed, by="t")


def test_r2_f4_records_the_caller_may_not_see_take_no_place(tmp_path: Path) -> None:
    env = Env(tmp_path)
    words = "flywheel flywheel flywheel bearing losses"
    hidden = [
        create_entry(sensitivity="restricted", title=f"Flywheel note {i}", text=words)
        for i in range(60)
    ]
    visible = create_entry(title="One visible", text="A short line about flywheel bearings.")
    seq = env.svc.project_info(env.project_id, AGENT)["project_seq"]
    done = env.svc.apply(env.project_id, request(env.project_id, [*hidden, visible], seq),
                         Caller("o", scope="owner"))  # fmt: skip
    assert done["ok"], done
    conn = ProjectStore(env.store_dir).connect()
    try:
        ranked = rt.rank(conn, AGENT, "flywheel bearing", kinds=("entry",), k=3, embedder=None)
    finally:
        conn.close()
    assert ranked["ids"] == [visible["id"]]


def test_b9_a_query_without_words_ranks_nothing(world: tuple[Env, dict[str, str]]) -> None:
    env, _ = world
    conn = ProjectStore(env.store_dir).connect()
    try:
        assert rt.rank(conn, AGENT, "?? !", kinds=("entry",), k=3, embedder=None)["ids"] == []
    finally:
        conn.close()


def test_r3_f4_the_same_queries_under_a_new_name_cannot_lower_the_target(
    world: tuple[Env, dict[str, str]],
) -> None:
    env, ids = world
    queries = [{"query_id": "q1", "text": "flywheel", "relevant": [ids["Flywheel losses"]]}]
    rt.fix(ProjectStore(env.store_dir), record(name="strict", queries=queries,
                                               recall_target=0.9), by="t")  # fmt: skip
    with pytest.raises(PrometheosError, match="never change"):
        rt.fix(ProjectStore(env.store_dir), record(name="lenient", queries=queries,
                                                   recall_target=0.1), by="t")  # fmt: skip
