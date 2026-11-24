"""Score family classes against golden labels at the recorded targets (C20-ADOPT; decision 0085).

    uv run python tools/adoption_class_score.py [--out FILE] [--seed N ...]

For each seed in ``tools/adoption_class_targets.json`` (or the seeds given), generate the forking
corpus in a temporary folder, adopt it in a disposable PrometheOS home through the application
service, classify every family and compare with the golden classes. Per-class precision and
recall are micro-averaged over the seeds and checked against the targets, which were recorded
before the first measurement and are never lowered. ``compete_hidden`` counts golden competing
families placed in a class that files no owner item; its target is zero.

Exit 0 only when every target is met. Synthetic data only; nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from adoption_fork_corpus import generate  # noqa: E402
from adoption_score import _all, row_path  # noqa: E402

from prometheos.adoption import classes  # noqa: E402
from prometheos.app.reads import Caller  # noqa: E402
from prometheos.app.service import Service  # noqa: E402

TARGETS = ROOT / "tools" / "adoption_class_targets.json"
AGENT = Caller(actor="class-score", scope="agent")


def adopt(seed: int, work: Path) -> dict[str, Any]:
    root = work / f"fork-{seed}"
    golden = generate(root, seed)
    svc = Service(home=work / f"home-{seed}", actor="class-score")
    pid = svc.init_project(
        title=f"Class score, seed {seed}",
        objective="Measure family classes on a synthetic forking repository.",
        roots={"corpus": str(root)},
    )["project_id"]
    scan = svc.adoption_scan(pid, AGENT, root_alias="corpus", mode="wait")
    gen = scan["scan_gen"]
    rows = _all(lambda o: svc.adoption_files(pid, AGENT, gen, offset=o, limit=500), "files")
    families = [
        svc.adoption_family(pid, AGENT, gen, f["family_id"])
        for f in _all(
            lambda o: svc.adoption_families(pid, AGENT, gen, offset=o, limit=500), "families"
        )
    ]
    return {"golden": golden, "rows": {r["path"]: r for r in rows}, "families": families}


def measure(result: dict[str, Any]) -> dict[str, Any]:
    gold = {
        frozenset(row_path(p) for p in f["members"]): f["class"]
        for f in result["golden"]["families"]
    }
    predicted: list[tuple[str | None, str]] = []  # (golden class or None, predicted class)
    seen: set[frozenset[str]] = set()
    for family in result["families"]:
        members = frozenset(m["path"] for m in family["members"])
        got = classes.classify(family, result["rows"])
        predicted.append((gold.get(members), got["class"]))
        if members in gold:
            seen.add(members)
    missing = [gold[m] for m in gold if m not in seen]  # golden families fusion did not form
    return {"predicted": predicted, "missing": missing}


def aggregate(measured: list[dict[str, Any]]) -> dict[str, Any]:
    per: dict[str, dict[str, int]] = {}
    hidden = 0
    for m in measured:
        for gold, got in m["predicted"]:
            for cls in {gold, got} - {None}:
                per.setdefault(str(cls), {"tp": 0, "fp": 0, "fn": 0})
            if gold == got:
                per[got]["tp"] += 1
            else:
                per[got]["fp"] += 1
                if gold is not None:
                    per[gold]["fn"] += 1
            if gold == "compete" and got != "compete":
                hidden += 1
        for gold in m["missing"]:
            per.setdefault(gold, {"tp": 0, "fp": 0, "fn": 0})["fn"] += 1
            if gold == "compete":
                hidden += 1
    out = {}
    for cls, c in sorted(per.items()):
        precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 1.0
        recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 1.0
        out[cls] = {**c, "precision": round(precision, 4), "recall": round(recall, 4)}
    return {"per_class": out, "compete_hidden": hidden}


def check(report: dict[str, Any], targets: dict[str, Any]) -> list[str]:
    failures = []
    for cls, goal in targets["per_class"].items():
        got = report["per_class"].get(cls, {"precision": 1.0, "recall": 0.0})
        for metric in ("precision", "recall"):
            if got[metric] < goal[metric]:
                failures.append(f"{cls} {metric} {got[metric]} < {goal[metric]}")
    if report["compete_hidden"] > targets["compete_hidden"]["max"]:
        failures.append(f"compete_hidden {report['compete_hidden']} > 0")
    return failures


def run(seeds: list[int] | None = None) -> dict[str, Any]:
    targets = json.loads(TARGETS.read_text(encoding="utf-8"))
    chosen = seeds or targets["seeds"]
    with tempfile.TemporaryDirectory(prefix="class-score-") as tmp:
        measured = [measure(adopt(seed, Path(tmp))) for seed in chosen]
    report = aggregate(measured) | {"seeds": chosen}
    report["failures"] = check(report, targets)
    report["errors"] = [
        {"seed": s, "golden": g, "predicted": p}
        for s, m in zip(chosen, measured, strict=True)
        for g, p in m["predicted"]
        if g != p
    ] + [
        {"seed": s, "golden": g, "predicted": "no family"}
        for s, m in zip(chosen, measured, strict=True)
        for g in m["missing"]
    ]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = run(args.seed)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
