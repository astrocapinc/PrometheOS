"""Score adoption against golden labels at the recorded targets (C17-LINEAGE; decision 0041).

    uv run python tools/adoption_score.py [--out FILE] [--seed N ...]

For each seed recorded in ``tools/adoption_targets.json`` (or the seeds given), generate the
synthetic corpus in a temporary folder, adopt it in a disposable PrometheOS home (inventory,
analyzers, fusion, through the application service), and compare families, current members and
every planted trap with the golden labels. Metrics are micro-averaged over the seeds and checked
against the targets, which were recorded before the first measurement and are never lowered.
Exit 0 only when every target is met. Synthetic data only; nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from adoption_corpus import CORPUS_LIMITS, generate  # noqa: E402

from prometheos.adoption import inventory as inv  # noqa: E402
from prometheos.adoption import scan as scan_mod  # noqa: E402
from prometheos.adoption.contracts import escape_segment  # noqa: E402
from prometheos.app.reads import Caller  # noqa: E402
from prometheos.app.service import Service  # noqa: E402

TARGETS = ROOT / "tools" / "adoption_targets.json"
AGENT = Caller(actor="score", scope="agent")
DECIDING_BANDS = ("decided", "supported")


def row_path(rel: str) -> str:
    return "/".join(escape_segment(p) for p in rel.split("/"))


def placeholder_provider(root: Path, marked: set[str]) -> inv.StatProvider:
    """Cloud placeholders cannot be made by user-mode code: the stat the inventory sees is
    marked instead (the same simulation as the tests)."""
    base = str(root.resolve())

    def stat_of(entry: Any) -> inv.Stat:
        st = inv.entry_stat(entry)
        full = entry.path.removeprefix("\\\\?\\")
        rel = Path(full).resolve().relative_to(base).as_posix()
        if rel in marked:
            bits = st.attributes | inv.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
            return inv.Stat(st.kind, st.size, st.mtime_ns, bits, st.reparse_tag)
        return st

    return stat_of


def adopt(seed: int, work: Path) -> dict[str, Any]:
    root = work / f"corpus-{seed}"
    golden = generate(root, seed)
    svc = Service(home=work / f"home-{seed}", actor="score")
    pid = svc.init_project(
        title=f"Adoption score, seed {seed}",
        objective="Measure adoption on a synthetic corpus.",
        roots={"corpus": str(root)},
    )["project_id"]
    marked = set(golden["placeholders"]) | set(golden["placeholder_folders"])
    original = scan_mod.STAT_PROVIDER
    scan_mod.STAT_PROVIDER = placeholder_provider(root, marked)
    try:
        scan = svc.adoption_scan(pid, AGENT, root_alias="corpus", limits=CORPUS_LIMITS, mode="wait")
    finally:
        scan_mod.STAT_PROVIDER = original
    gen = scan["scan_gen"]
    rows = _all(lambda o: svc.adoption_files(pid, AGENT, gen, offset=o, limit=500), "files")
    evidence = _all(
        lambda o: svc.adoption_evidence(pid, AGENT, gen, offset=o, limit=500), "evidence"
    )
    families = [
        svc.adoption_family(pid, AGENT, gen, f["family_id"])
        for f in _all(
            lambda o: svc.adoption_families(pid, AGENT, gen, offset=o, limit=500), "families"
        )
    ]
    return {
        "golden": golden,
        "scan": scan,
        "rows": {r["path"]: r for r in rows},
        "evidence": evidence,
        "families": families,
    }


def _all(page: Callable[[int], dict[str, Any]], key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        current = page(offset)
        items += current[key]
        offset = current["next_offset"]
    return items


# ----- metrics -------------------------------------------------------------------------------


def _pairs(groups: list[set[str]], universe: set[str]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for group in groups:
        inside = sorted(group & universe)
        out |= set(itertools.combinations(inside, 2))
    return out


def family_of(families: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    for family in families:
        if any(m["path"] == path for m in family["members"]):
            return family
    return None


def measure(result: dict[str, Any]) -> dict[str, Any]:
    golden, families = result["golden"], result["families"]
    gold_groups = [{row_path(p) for p in f["members"]} for f in golden["families"]]
    universe = set().union(*gold_groups)
    predicted = [{m["path"] for m in f["members"]} for f in families]
    gold_pairs, pred_pairs = _pairs(gold_groups, universe), _pairs(predicted, universe)
    hit = len(gold_pairs & pred_pairs)
    correct = decided = 0
    for g in golden["families"]:
        family = family_of(families, row_path(g["current"]))
        if family is not None and family["band"] in DECIDING_BANDS:
            decided += 1
            correct += int(family["current"] == row_path(g["current"]))
    reasons = [r for f in families for m in f["members"] for r in m["reasons"]]
    traps = {t["id"]: TRAP_CHECKS.get(t["id"], _unknown)(t, result) for t in golden["traps"]}
    return {
        "pairs": {"golden": len(gold_pairs), "predicted": len(pred_pairs), "agree": hit},
        "current": {
            "golden_families": len(golden["families"]),
            "decided_or_supported": decided,
            "correct": correct,
        },
        "traps": traps,
        "owner_rule_without_owner": sum(1 for r in reasons if r["rule"] == "R1"),
        "mtime_reasons": sum(
            1
            for r in reasons
            if "modification time" in r["text"].lower() or "mtime" in r["text"].lower()
        ),
    }


# ----- trap checks: detected as specified or surfaced as unresolved --------------------------


def _unknown(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    return False  # a trap without a check is never counted as handled


def _not_current(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    for path in trap["paths"]:
        family = family_of(result["families"], row_path(path))
        if family is None or family["current"] == row_path(path):
            return False
    return True


def _excluded_by(predicate: str) -> Callable[[dict[str, Any], dict[str, Any]], bool]:
    def check(trap: dict[str, Any], result: dict[str, Any]) -> bool:
        for path in trap["paths"]:
            family = family_of(result["families"], row_path(path))
            member = (
                next((m for m in family["members"] if m["path"] == row_path(path)), None)
                if family
                else None
            )
            if member is None or member["outcome"] != "excluded":
                return False
            if not any(
                e["subject"] == row_path(path) and e["predicate"] == predicate
                for e in result["evidence"]
            ):
                return False
        return True

    return check


def _rows_status(*statuses: str) -> Callable[[dict[str, Any], dict[str, Any]], bool]:
    def check(trap: dict[str, Any], result: dict[str, Any]) -> bool:
        return all(
            (row := result["rows"].get(row_path(p))) is not None and row["status"] in statuses
            for p in trap["paths"]
        )

    return check


def _evidence(
    predicate: str, literal: Any = None
) -> Callable[[dict[str, Any], dict[str, Any]], bool]:
    def check(trap: dict[str, Any], result: dict[str, Any]) -> bool:
        return all(
            any(
                e["subject"] == row_path(p)
                and e["predicate"] == predicate
                and (
                    literal is None
                    or e["object"].get("literal") == literal
                    or (
                        isinstance(e["object"].get("literal"), dict)
                        and e["object"]["literal"].get("level") == literal
                    )
                )
                for e in result["evidence"]
            )
            for p in trap["paths"]
        )

    return check


def _placeholder(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    golden, rows = result["golden"], result["rows"]

    def status(path: str) -> tuple[str | None, str | None]:
        row = rows.get(row_path(path))
        return (row["entry"], row["status"]) if row else (None, None)

    files_ok = all(status(p)[1] == "placeholder" for p in golden["placeholders"])
    folders_ok = all(status(p) == ("folder", "placeholder") for p in golden["placeholder_folders"])
    hidden_ok = all(row_path(p) not in rows for p in golden["unlisted"])
    return files_ok and folders_ok and hidden_ok


def _contradiction(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    ids = {e["evidence_id"]: e["subject"] for e in result["evidence"]}
    notes = {row_path(p) for p in trap["paths"]}
    for family in result["families"]:
        for conflict in family["conflicts"]:
            sources = {ids.get(i) for i in conflict["evidence"]}
            if notes <= sources:
                return True
    return False


def _nested(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    inner = [
        r for r in result["rows"].values() if r["parent"] and r["parent"].endswith("inner.zip")
    ]
    return bool(inner) and all(r["depth"] == 2 and r["status"] == "listed" for r in inner)


def _limited(prefix: str) -> Callable[[dict[str, Any], dict[str, Any]], bool]:
    def check(trap: dict[str, Any], result: dict[str, Any]) -> bool:
        return any(
            r["status"] == "limited"
            and (r["reason"] or "").startswith(prefix)
            and r["path"].startswith(row_path(trap["paths"][0]))
            for r in result["rows"].values()
        )

    return check


def _cache(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    path = row_path(trap["paths"][0])
    linked = any(e["subject"] == path and e["predicate"] == "cache_of" for e in result["evidence"])
    return linked and family_of(result["families"], path) is None


def _injection(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    path = row_path(trap["paths"][0])
    no_statement = not any(
        e["subject"] == path and e["predicate"] == "has_status" for e in result["evidence"]
    )
    flagged = any(
        e["subject"] == path and e["object"].get("facet") == "note.hidden_instructions"
        for e in result["evidence"]
    )
    return no_statement and flagged


def _odd_members(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    archive = row_path(trap["paths"][0])
    members = [r for r in result["rows"].values() if r["parent"] == archive]
    return len(members) == 5 and len({m["path"] for m in members}) == 5


def _upgrade(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    return _excluded_by("backup_of")(trap, result) and _evidence("same_origin_as")(trap, result)


def _status_phrasing(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    """The newest file stays current, the older two are excluded, the family is not decided on
    statements alone, and the negated line is kept as an unresolved statement."""
    newest = row_path(trap["paths"][0])
    family = family_of(result["families"], newest)
    if family is None or family["current"] != newest or family["band"] == "decided":
        return False
    outcomes = {m["path"]: m["outcome"] for m in family["members"]}
    older = [p for p in outcomes if p != newest]
    if not older or any(outcomes[p] != "excluded" for p in older):
        return False
    return any(
        e["predicate"] == "unresolved"
        and e["object"].get("path") == newest
        and (e["object"].get("literal") or {}).get("kind") == "status_statement"
        for e in result["evidence"]
    )


def _no_family(trap: dict[str, Any], result: dict[str, Any]) -> bool:
    return all(family_of(result["families"], row_path(p)) is None for p in trap["paths"])


TRAP_CHECKS: dict[str, Callable[[dict[str, Any], dict[str, Any]], bool]] = {
    "trivial_files": _no_family,
    "status_phrasing": _status_phrasing,
    "hidden_markdown": _injection,
    "tag_characters": _injection,
    "abandoned_caller": _not_current,
    "mtime_rewrite": _not_current,
    "line_endings": _evidence("copy_of", "normalised"),
    "final_name": _not_current,
    "best_name": _not_current,
    "contradiction": _contradiction,
    "placeholder": _placeholder,
    "nested_archive": _nested,
    "zip_bomb": _limited("max_ratio"),
    "deep_archive": _limited("max_depth"),
    "too_many_members": _limited("max_members"),
    "objective_c": _evidence("language", "objective-c"),
    "long_path": _rows_status("read"),
    "non_ascii": _rows_status("read"),
    "bang_name": _rows_status("read"),
    "autosave": _excluded_by("autosave_of"),
    "upgrade_backup": _upgrade,
    "cache": _cache,
    "injection": _injection,
    "odd_member_names": _odd_members,
    "corrupt_package": _rows_status("unreadable"),
}


# ----- aggregate and compare -----------------------------------------------------------------


def aggregate(per_seed: dict[int, dict[str, Any]]) -> dict[str, float]:
    gold = sum(m["pairs"]["golden"] for m in per_seed.values())
    pred = sum(m["pairs"]["predicted"] for m in per_seed.values())
    agree = sum(m["pairs"]["agree"] for m in per_seed.values())
    families = sum(m["current"]["golden_families"] for m in per_seed.values())
    decided = sum(m["current"]["decided_or_supported"] for m in per_seed.values())
    correct = sum(m["current"]["correct"] for m in per_seed.values())
    traps = [ok for m in per_seed.values() for ok in m["traps"].values()]
    return {
        "family_pairwise_precision": round(agree / pred, 4) if pred else 0.0,
        "family_pairwise_recall": round(agree / gold, 4) if gold else 0.0,
        "current_precision": round(correct / decided, 4) if decided else 0.0,
        "current_coverage": round(decided / families, 4) if families else 0.0,
        "traps_handled": round(sum(traps) / len(traps), 4) if traps else 0.0,
        "auto_applied_supersessions": sum(m["owner_rule_without_owner"] for m in per_seed.values()),
        "mtime_decisions": sum(m["mtime_reasons"] for m in per_seed.values()),
    }


def compare(metrics: dict[str, float], targets: dict[str, dict[str, float]]) -> list[str]:
    failures = []
    for name, bound in targets.items():
        value = metrics[name]
        if "min" in bound and value < bound["min"]:
            failures.append(f"{name} {value} < {bound['min']}")
        if "max" in bound and value > bound["max"]:
            failures.append(f"{name} {value} > {bound['max']}")
    return failures


def run(seeds: list[int] | None = None) -> dict[str, Any]:
    recorded = json.loads(TARGETS.read_text("utf-8"))
    seeds = seeds or recorded["seeds"]
    work = Path(tempfile.mkdtemp(prefix="prometheos-adoption-score-"))
    try:
        per_seed = {seed: measure(adopt(seed, work)) for seed in seeds}
    finally:
        shutil.rmtree(inv.os_path(work), ignore_errors=True)
    metrics = aggregate(per_seed)
    failures = compare(metrics, recorded["targets"])
    return {
        "seeds": seeds,
        "metrics": metrics,
        "targets": recorded["targets"],
        "failures": failures,
        "per_seed": per_seed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seed", type=int, action="append")
    args = parser.parse_args(argv)
    report = run(args.seed)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"metrics": report["metrics"], "failures": report["failures"]}, indent=2))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
