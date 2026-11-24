"""C17-LINEAGE, analyzer half (A05, CP17.2; decision 0040): the deterministic analyzer pack,
including the MATLAB/Simulink module, produces typed evidence with tiers on the seeded corpus.
MATLAB call and model-reference edges match the generator's golden edges (and MATLAB's own
dependency analysis, checked once on a scratch copy: decision 0040); the Objective-C ``.m`` is
not parsed as MATLAB; autosaves, upgrade backups, caches, copies, project membership and the
notes' status statements are evidence with verbatim quotes; hidden instructions in a note are
flagged, never read as statements; an existing Graphify graph counts only where its files are
fresh; re-analysis replays the cache and gives the same evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from adoption_corpus import CORPUS_LIMITS, generate
from cp1_support import AGENT, Env
from test_cp17_inventory import _add_root, placeholder_provider, placeholders_of

from prometheos.adoption import analyzers as az
from prometheos.adoption import matlab
from prometheos.adoption import scan as scan_mod
from prometheos.domain.models import PrometheosError

SEED = 17


@pytest.fixture(scope="module")
def adopted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    base = tmp_path_factory.mktemp("analyzed")
    root = base / "corpus"
    golden = generate(root, SEED)
    (base / "env").mkdir()
    env = Env(base / "env")
    _add_root(env, "corpus", root)
    original = scan_mod.STAT_PROVIDER
    scan_mod.STAT_PROVIDER = placeholder_provider(root, placeholders_of(golden))
    try:
        scan = env.svc.adoption_scan(
            env.project_id, AGENT, root_alias="corpus", limits=CORPUS_LIMITS, mode="wait"
        )
    finally:
        scan_mod.STAT_PROVIDER = original
    evidence: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = env.svc.adoption_evidence(
            env.project_id, AGENT, scan["scan_gen"], offset=offset, limit=500
        )
        evidence += page["evidence"]
        offset = page["next_offset"]
    return {"env": env, "root": root, "golden": golden, "scan": scan, "evidence": evidence}


def has(
    evidence: list[dict[str, Any]],
    subject: str,
    predicate: str,
    target: str | None = None,
    tier: str | None = None,
) -> list[dict[str, Any]]:
    return [
        e
        for e in evidence
        if e["subject"] == subject
        and e["predicate"] == predicate
        and (
            target is None
            or e["object"].get("path") == target
            or e["object"].get("external") == target
        )
        and (tier is None or e["tier"] == tier)
    ]


def facet(evidence: list[dict[str, Any]], subject: str, name: str) -> Any:
    found = [e for e in has(evidence, subject, "facet") if e["object"]["facet"] == name]
    return found[0]["object"]["literal"] if found else None


def test_the_scan_records_its_analysis(adopted: dict[str, Any]) -> None:
    scan = adopted["scan"]
    assert scan["state"] == "complete" and scan["analysis"]["evidence"] == len(adopted["evidence"])
    assert scan["analysis"]["extracted"] > 0 and scan["analysis"]["changed_since_inventory"] == 0
    assert all(e["tier"] != "T0" for e in adopted["evidence"]), "no analyzer speaks for the owner"


def test_matlab_edges_match_the_golden_labels(adopted: dict[str, Any]) -> None:
    ev, golden = adopted["evidence"], adopted["golden"]
    for edge in golden["edges"]:
        predicate, source, target = edge["predicate"], edge["from"], edge["to"]
        if predicate == "calls":
            assert has(ev, source, "calls", f"analysis/{target}.m", "T3"), edge
        elif predicate == "used":
            assert has(ev, source, "used", target, "T6"), edge
        elif predicate == "generated_by":
            assert has(ev, source, "generated_by", target, "T6"), edge
        elif predicate == "model_reference":
            hits = [
                e
                for e in has(ev, source, "model_reference")
                if e["object"]["path"].rsplit("/", 1)[-1].split(".")[0] == target.rsplit("/", 1)[-1]
            ]
            assert hits, edge
        elif predicate == "library_link":
            assert has(ev, source, "library_link", f"simulink:{target}", "T3"), edge
        elif predicate == "cache_of":
            assert has(ev, source, "cache_of", "models/plant.slx", "T3"), edge
    # a model referenced by name resolves to the file MATLAB would load, not to a dated backup
    refs = {e["object"]["path"] for e in has(ev, "models/controller.mdl", "model_reference")}
    assert refs == {"models/plant.slx"}
    call = has(ev, "analysis/run_sweep.m", "calls", "analysis/compute_metrics.m")[0]
    line = (adopted["root"] / "analysis/run_sweep.m").read_text("utf-8").splitlines()
    assert call["quote"] == line[call["locator"]["start"] - 1].strip()
    assert call["quote_sha256"] == hashlib.sha256(call["quote"].encode()).hexdigest()


def test_objective_c_is_not_parsed_as_matlab(adopted: dict[str, Any]) -> None:
    ev = adopted["evidence"]
    objc = has(ev, "analysis/ios/AppDelegate.m", "language")
    assert [e["object"]["literal"] for e in objc] == ["objective-c"]
    assert not has(ev, "analysis/ios/AppDelegate.m", "calls")
    assert [e["object"]["literal"] for e in has(ev, "analysis/run_sweep.m", "language")] == [
        "matlab"
    ]


def test_tool_conventions_and_package_identity(adopted: dict[str, Any]) -> None:
    ev = adopted["evidence"]
    assert has(ev, "analysis/run_sweep.asv", "autosave_of", "analysis/run_sweep.m", "T2")
    assert has(ev, "models/plant.slx.r2024b", "backup_of", "models/plant.slx", "T2")
    # one uuid: the upgrade backup and the dated backup share the model's origin (T1)
    origin = {e["subject"] for e in ev if e["predicate"] == "same_origin_as"}
    assert origin == {"models/plant.slx.r2024b", "models/plant_backup_2026_01_10.slx"}
    assert facet(ev, "models/plant.slx", "package.revision") == "2.4"
    assert facet(ev, "models/plant.slx.r2024b", "package.release") == "R2024b"
    assert facet(ev, "models/controller.mdl", "package.model_name") == "controller"
    assert facet(ev, "results/sweep_v1.mat", "mat.created") == "2026-01-15T09:00:00"
    assert facet(ev, "results/sweep_FINAL.mat", "mat.variables") == ["peak"]


def test_copies_and_names(adopted: dict[str, Any]) -> None:
    ev = adopted["evidence"]
    copy = has(
        ev, "backup/analysis_copy/compute_metrics.m", "copy_of", "analysis/compute_metrics.m", "T1"
    )
    assert copy and copy[0]["object"]["literal"] == {"level": "normalised"}
    stem = facet(ev, "results/sweep_FINAL.mat", "name.stem")
    assert stem == {"stem": "sweep", "tokens": ["final"]}
    assert facet(ev, "models/plant_backup_2026_01_10.slx", "name.stem")["stem"] == "plant"
    assert facet(ev, "models/legacy/controller_2019.mdl", "name.stem")["stem"] == "controller"


def test_project_membership_and_labels(adopted: dict[str, Any]) -> None:
    ev, labels = adopted["evidence"], adopted["golden"]["project"]["labels"]
    for member, label in labels.items():
        found = has(ev, member, "project_file", "SynthSweep.prj", "T3")
        assert found and found[0]["object"]["literal"] == {"label": label}, member
    assert not has(ev, "SynthSweep.prj", "unresolved")


def test_statements_carry_quotes_and_contradictions_are_kept(adopted: dict[str, Any]) -> None:
    ev, golden = adopted["evidence"], adopted["golden"]
    current = next(f["current"] for f in golden["families"] if f["id"] == "sweep_results")
    log = has(ev, "notes/decision-log.md", "has_status")
    by_target = {e["object"]["path"]: e["object"]["literal"] for e in log}
    assert by_target[current] == {"status": "current", "date": "2026-03-10"}
    assert by_target["results/sweep_FINAL.mat"] == {"status": "superseded", "date": "2026-03-10"}
    assert by_target["models/plant.slx"]["status"] == "current"
    old = {
        e["object"]["path"]: e["object"]["literal"]
        for e in has(ev, "notes/status-old.md", "has_status")
    }
    assert old["results/sweep_FINAL.mat"]["status"] == "final_claimed", "both statements are kept"
    text = (adopted["root"] / "notes/decision-log.md").read_text("utf-8").splitlines()
    for e in log:
        assert e["tier"] == "T4" and e["quote"] == text[e["locator"]["start"] - 1].strip()


def test_hidden_instructions_are_flagged_not_read(adopted: dict[str, Any]) -> None:
    ev = adopted["evidence"]
    assert not has(ev, "notes/meeting-notes.md", "has_status"), "hidden text is no statement"
    assert facet(ev, "notes/meeting-notes.md", "note.hidden_instructions") == 1


def test_graphify_edges_count_only_when_fresh(adopted: dict[str, Any]) -> None:
    ev, expected = adopted["evidence"], adopted["golden"]["graphify"]
    graphify = [e for e in ev if e["analyzer"] == "graphify.import"]
    ((source, target),) = expected["fresh_calls"]
    assert [
        e
        for e in graphify
        if e["subject"] == source
        and e["predicate"] == "calls"
        and e["object"]["path"] == target
        and e["tier"] == "T3"
    ]
    stale = expected["stale"][0]
    assert not [
        e for e in graphify if e["predicate"] == "calls" and e["object"].get("path") == stale
    ]
    assert facet(ev, stale, "graphify.freshness") == "stale"
    ((note, code),) = expected["inferred"]
    hint = [e for e in graphify if e["subject"] == note and e["object"].get("path") == code]
    assert hint and hint[0]["tier"] == "T8"
    assert "researcher" not in repr(graphify), "the manifest's absolute paths are not stored"


def test_rescan_replays_the_cache_and_gives_the_same_evidence(adopted: dict[str, Any]) -> None:
    env, scan = adopted["env"], adopted["scan"]
    original = scan_mod.STAT_PROVIDER
    scan_mod.STAT_PROVIDER = placeholder_provider(
        adopted["root"], placeholders_of(adopted["golden"])
    )
    try:
        again = env.svc.adoption_scan(
            env.project_id, AGENT, root_alias="corpus", limits=CORPUS_LIMITS, mode="wait"
        )
    finally:
        scan_mod.STAT_PROVIDER = original
    assert again["analysis"]["extracted"] == 0
    assert again["analysis"]["replayed"] == scan["analysis"]["extracted"]
    second = env.svc.adoption_evidence(env.project_id, AGENT, again["scan_gen"], limit=500)
    first = env.svc.adoption_evidence(env.project_id, AGENT, scan["scan_gen"], limit=500)

    def strip(items: list[dict[str, Any]]) -> list[Any]:
        return sorted((e["subject"], e["predicate"], repr(e["object"]), e["tier"]) for e in items)

    assert strip(second["evidence"]) == strip(first["evidence"])


def test_evidence_reads_validate_their_filters(adopted: dict[str, Any]) -> None:
    env, gen = adopted["env"], adopted["scan"]["scan_gen"]
    about = env.svc.adoption_evidence(env.project_id, AGENT, gen, path="analysis/compute_metrics.m")
    assert {e["predicate"] for e in about["evidence"]} >= {"calls", "copy_of", "language"}
    for bad in ({"predicate": "owns"}, {"tier": "T9"}, {"analyzer": "nope.x"}):
        with pytest.raises(PrometheosError) as err:
            env.svc.adoption_evidence(env.project_id, AGENT, gen, **bad)
        assert err.value.code == "validation_error"
    names = {a["id"] for a in env.svc.adoption_analyzers(env.project_id, AGENT)["analyzers"]}
    assert names == set(az.ANALYZERS)


# ----- the MATLAB/Simulink module in isolation -----------------------------------------------


def test_language_rules_follow_linguist_order() -> None:
    assert matlab.language_of("#import <Foundation/Foundation.h>\n") == "objective-c"
    assert (
        matlab.language_of("@interface A : NSObject\n@end\n% not a comment here\n") == "objective-c"
    )
    assert matlab.language_of("% a script\nx = 1;\n") == "matlab"
    assert matlab.language_of("function y = f(x)\ny = x;\nend\n") == "matlab"
    assert matlab.language_of("classdef Car < handle\nend\n") == "matlab"
    # A bare script that parses cleanly and assigns is MATLAB (review cp17-234-a F17); plain
    # words and C stay unknown.
    assert matlab.language_of("x = 1;\ny = 2;\n") == "matlab"
    assert matlab.language_of("clear; clc\nx = readmatrix('data.csv');\n") == "matlab"
    assert matlab.language_of("hello world\n") == "unknown"
    assert matlab.language_of("int main() { return 0; }\n") == "unknown"
    assert matlab.language_of(":- module foo.\n") == "unknown"


def test_matlab_structure_drops_variables_and_reads_commands() -> None:
    source = (
        b"function [a, b] = f(x, y)\nfor k = 1:3\n  m(k) = helper(x);\nend\n"
        b"load data/in.mat\nsave('out/res.mat', 'm');\ns.f = g(2);\n"
        b"open_system('models/plant')\nend\n"
    )
    s = matlab.parse_matlab(source)
    calls = {c.name for c in s.calls} - s.assigned
    assert calls == {"helper", "g"}
    assert [r.name for r in s.reads] == ["data/in.mat"]
    assert [w.name for w in s.writes] == ["out/res.mat"]
    assert [m.name for m in s.models] == ["models/plant"]
    assert s.errors == 0 and [d.name for d in s.definitions] == ["f"]


def test_graphify_shaped_extraction() -> None:
    out = matlab.extract_matlab(
        "a/run.m", b"% run\nx = helper(1);\nfunction y = helper(z)\ny = z;\nend\n"
    )
    node_keys = {"id", "label", "file_type", "source_file", "source_location"}
    edge_keys = {
        "source",
        "target",
        "relation",
        "confidence",
        "confidence_score",
        "source_file",
        "source_location",
        "weight",
    }
    assert all(node_keys <= set(n) for n in out["nodes"])
    assert all(edge_keys == set(e) for e in out["edges"])
    assert {(e["relation"], e["target"]) for e in out["edges"]} == {
        ("contains", "a_run_m_helper"),
        ("calls", "a_run_m_helper"),
    }
    objc = matlab.extract_matlab("x.m", b"@interface A\n@end\n")
    assert objc["edges"] == [] and objc["nodes"][0]["language"] == "objective-c"


def test_classic_mdl_and_package_text_are_read() -> None:
    classic = (
        'Model {\n  Name "ctrl"\n  System {\n    Name "ctrl"\n    Block {\n'
        '      BlockType ModelReference\n      Name "P"\n      ModelNameDialog "plant"\n    }\n'
        '    Block {\n      BlockType Reference\n      Name "L"\n'
        '      SourceBlock "mylib/Thing"\n    }\n  }\n}\n'
    )
    package = matlab.read_mdl(classic)
    assert package.model_name == "ctrl" and package.model_references == ["plant"]
    assert package.library_links == ["mylib/Thing"]
    assert matlab.read_mdl("hello world").problems == ["not a Simulink model text"]


def test_xml_with_entities_is_refused() -> None:
    hostile = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><System>&a;</System>'
    with pytest.raises(Exception, match="refused"):
        matlab.safe_xml(hostile)
    package = matlab.Package()
    matlab._blocks(hostile, package)
    assert package.problems and not package.model_references


def test_mat_header_reader_handles_truncation_and_garbage() -> None:
    assert matlab.read_mat_header(b"garbage").version == "v4"
    header = matlab.read_mat_header(
        b"MATLAB 5.0 MAT-file, Platform: PCWIN64, Created on: Mon Jan  5 09:00:00 2026".ljust(116)
        + b"\0" * 8
        + b"\x00\x01IM"
        + b"\x0e\x00\x00\x00\xff\xff\x00\x00"
    )
    assert (
        header.version == "v5"
        and header.created == "2026-01-05T09:00:00"
        and header.variables == []
    )


def test_name_stems() -> None:
    assert az.name_stem("results/sweep_v3.mat") == ("sweep", ["v3"])
    assert az.name_stem("x/gearbox_backup_4_5_26.slx")[0] == "gearbox"
    assert az.name_stem("m/plant.slx.r2024b") == ("plant", [])
    assert az.name_stem("a/report - Copy (2).docx")[0] == "report"
    assert az.name_stem("a/FINAL.md")[0] == "final", "a name that is only a token stays itself"


def test_analyzer_declarations_are_contracts() -> None:
    for analyzer in az.ANALYZERS.values():
        expected = {"ai_task": "proposed", "agent_task": "reported"}.get(analyzer.kind, "pure")
        assert analyzer.determinism == expected, analyzer.id
        assert "T0" not in analyzer.tiers and analyzer.hydrates_placeholders is False
    assert Path(az.__file__).read_text("utf-8").count("hydrates_placeholders=True") == 0
