"""Measure a registered intelligence-layer task on its golden set (A07.7, CP22.3; decision 0093).

    uv run python tools/ai_eval.py --record RECORD.json --cassettes DIR --out RESULT.json

Replays recorded reply bodies (cassettes: ``{"task_kind", "input_sha256", "sample", "body"}``,
one JSON file each) over the evaluation record's golden set in a disposable project store, and
writes the measurement: coverage, precision and its one-sided 95% Clopper-Pearson lower bound
overall and per outcome, and whether the targets are met. The record must be a development
record: an acceptance record is measured once, through the gateway (decision 0105, K1), so a
replay cannot spend it. A replay never confers trust (only
gateway replies bound to their call receipts do), so this tool is free, deterministic and makes
no call. A live measurement is gate E21: it runs only with the owner's OK, the owner's key and a
spend cap, through the layer's own evaluation operation, never from this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from prometheos.app.service import Service
from prometheos.intelligence import evaluation, tasks
from prometheos.intelligence.run import CassetteExecutor
from prometheos.store.project import ProjectStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", type=Path, required=True, help="the evaluation record (JSON)")
    parser.add_argument("--cassettes", type=Path, required=True, help="folder of recorded replies")
    parser.add_argument("--out", type=Path, required=True, help="where the measurement is written")
    args = parser.parse_args(argv)
    record = evaluation.EvaluationRecord.model_validate(
        json.loads(args.record.read_text(encoding="utf-8"))
    )
    task = tasks.get(record.task_kind)
    with tempfile.TemporaryDirectory(prefix="prometheos-ai-eval-") as work:
        service = Service(home=Path(work) / "home", actor="ai-eval")
        made = service.init_project(title="Evaluation (disposable)", objective="Measure a task.",
                                    timezone="UTC")  # fmt: skip
        store = ProjectStore(Path(made["store"]))
        digest = evaluation.fix(store, record, by="tools/ai_eval.py")
        measured = evaluation.measure(
            store, task, digest, CassetteExecutor.from_folder(args.cassettes)
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: measured[k] for k in ("task_kind", "items", "coverage",
                                               "precision_lower_bound", "meets_targets",
                                               "trusted")}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
