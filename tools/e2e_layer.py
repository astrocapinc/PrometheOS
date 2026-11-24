"""Browser-test helper for the intelligence layer on records (decisions 0109, 0110). Test only.

``run``: record a cassette for each named subject's *current* bundle (two models, or one for a
one-sample task) and run the task over the named claims with them, as an agent would with
``prometheos ai map-suggest --cassettes``. Nothing is sent; cassette results confer no trust.
The hub keeps running: like the CLI, this is a second process on the same store.

``relations``: the stored ``answers`` relations of one claim, with their revisions, so a browser
test can assert what "Place it there" wrote.

``rerun`` and ``card`` set up, as an agent would (synthetic): a claim whose 196.9 MJ is bound in
the first version of ``<folder>/<name>``, then a rerun's new version reported in a bare receipt;
or such a claim with a withdrawal correction asked for with a decision card.

    python tools/e2e_layer.py run --home H --project P --task KIND --claim C --reply SUBJECT=JSON
    python tools/e2e_layer.py relations --home H --project P --claim C
    python tools/e2e_layer.py rerun --home H --project P --folder F --name run.log
    python tools/e2e_layer.py card --home H --project P --folder F --name card.log
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODELS = ("vendor-a/model", "vendor-b/model")
SCOPE = {
    "system": "Synthetic plant",
    "operating_regime": "Urban cycle",
    "comparator": "Same vehicle without the device",
    "model_fidelity": "Reduced model",
    "validation_level": "Re-derived from the stored artefact",
}
DEFAULT_TEXT = "Views show the claim as settling until you decide."
FIRST = "peak power = 412.5 kW\nenergy = 196.9 MJ\n"
SECOND = "peak power = 420.0 kW\nenergy = 201.3 MJ\n"


def _cassette(folder: Path, kind: str, sha: str, sample: int, reply: Any, model: str) -> None:
    body = {
        "id": "e2e",
        "model": model,
        "provider": "Recorded",
        "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}],
    }
    record = {"task_kind": kind, "input_sha256": sha, "sample": sample, "body": body,
              "model": model}  # fmt: skip
    (folder / f"{kind}-{sha[:12]}-{sample}.json").write_text(json.dumps(record), "utf-8")


class _Agent:
    def __init__(self, args: argparse.Namespace) -> None:
        from prometheos.app.reads import Caller
        from prometheos.app.service import Service

        self.svc = Service(home=Path(args.home), actor="e2e-agent")
        self.pid = str(args.project)
        self.agent = Caller(actor="e2e-agent", scope="agent")
        self.owner = Caller(actor="e2e-agent", scope="owner")

    def request(self, ops: list[dict[str, Any]], card: dict[str, Any] | None = None) -> bytes:
        from prometheos.domain.ids import new_request_id

        seq = int(self.svc.project_info(self.pid, self.owner)["project_seq"])
        body: dict[str, Any] = {"schema_version": 1, "project_id": self.pid,
                                "request_id": new_request_id(), "expected_seq": seq,
                                "operations": ops}  # fmt: skip
        if card is not None:
            body["card"] = card
        return json.dumps(body).encode("utf-8")

    def create(self, kind: str, rid: str, body: dict[str, Any]) -> None:
        op = {"op": "create", "kind": kind, "id": rid, "base_rev": 0, "sensitivity": "normal",
              "occurred_at": None, "body": body}  # fmt: skip
        self.svc.apply(self.pid, self.request([op]), self.agent)

    def version(self, folder: Path, name: str, text: str) -> dict[str, str]:
        (folder / name).write_text(text, "utf-8")
        plan = self.svc.import_plan(self.pid, root_alias="research", paths=[name],
                                    retention="copy", sensitivity="normal")  # fmt: skip
        self.svc.import_commit(self.pid, plan["plan_id"])
        (item,) = plan["items"]
        return {k: str(item[k]) for k in ("source_id", "version_id", "sha256")}

    def bound_claim(self, folder: Path, name: str) -> str:
        """A claim whose 196.9 MJ is bound on line 2 of the file's first version."""
        from prometheos.domain.ids import new_id

        first = self.version(folder, name, FIRST)
        ev = {
            "source_id": first["source_id"],
            "version_id": first["version_id"],
            "locator": {"type": "text", "start_line": 2, "end_line": 2},
            "quote_sha256": None,
        }
        binding = {"decimal": "196.9", "unit": "MJ", "source_ref": ev,
                   "source_pointer": "text:2:9:17", "name": "uphill energy"}  # fmt: skip
        cid = new_id("claim")
        self.create("claim", cid, {
            "statement": f"The uphill energy is 196.9 MJ ({name}, synthetic).", "scope": SCOPE,
            "evidence": [ev], "assessment_refs": [], "state": "proposed", "superseded_by": None,
            "quantities": [binding],
        })  # fmt: skip
        return cid


def run(args: argparse.Namespace) -> dict[str, Any]:
    from prometheos.app import intelligence as app_intelligence
    from prometheos.intelligence import tasks

    agent = _Agent(args)
    task = tasks.get(args.task)
    store = agent.svc.open(agent.pid)
    subjects = app_intelligence._record_subjects(task.kind)(store, agent.agent, args.claim)
    replies = dict(item.split("=", 1) for item in args.reply)
    with tempfile.TemporaryDirectory(prefix="e2e-layer-") as tmp:
        folder = Path(tmp)
        for subject in subjects:
            reply = replies.get(subject.bundle.subject) or replies.get("*")
            if reply is None:
                continue
            for sample in range(task.agreement):
                _cassette(folder, task.kind, subject.bundle.input_sha256, sample,
                          json.loads(reply), MODELS[sample])  # fmt: skip
        done = agent.svc.ai_map_suggest(agent.pid, agent.agent, task_kind=task.kind,
                                        claim_ids=args.claim, cassettes=folder)  # fmt: skip
    return {"subjects": [s.bundle.subject for s in subjects], "items": done["items"]}


def relations(args: argparse.Namespace) -> dict[str, Any]:
    store = _Agent(args).svc.open(str(args.project))
    conn = sqlite3.connect(f"file:{(store.directory / 'record.sqlite').as_posix()}?mode=ro",
                           uri=True)  # fmt: skip
    try:
        rows = conn.execute(
            "SELECT source_id, source_rev, target_id, target_rev FROM relations WHERE "
            "source_id = ? AND relation = 'answers' AND retired_seq IS NULL",
            (args.claim[0],),
        ).fetchall()
    finally:
        conn.close()
    return {"answers": [{"source_id": r[0], "source_rev": r[1], "target_id": r[2],
                         "target_rev": r[3]} for r in rows]}  # fmt: skip


def rerun(args: argparse.Namespace) -> dict[str, Any]:
    from prometheos.domain.ids import new_request_id

    agent = _Agent(args)
    folder = Path(args.folder)
    cid = agent.bound_claim(folder, args.name)
    new = agent.version(folder, args.name, SECOND)
    receipt = {
        "schema_version": 1, "observation": "reported", "reporter": "e2e agent",
        "command": "run('sweep.m')", "working_root": "research",
        "environment": {"os": "TestOS", "tools": []}, "started_at": None, "ended_at": None,
        "exit_status": 0, "outcome": "succeeded", "inputs": [],
        "outputs": [new | {"role": "result"}], "limitations": ["synthetic"],
    }  # fmt: skip
    done = agent.svc.receipt_record(agent.pid, raw=json.dumps(receipt).encode("utf-8"),
                                    title=f"Rerun of {args.name} (synthetic)", description="",
                                    request_id=new_request_id())  # fmt: skip
    return {"claim_id": cid, "new_version_id": new["version_id"], "receipt": done}


def card(args: argparse.Namespace) -> dict[str, Any]:
    from prometheos.domain.ids import new_id
    from prometheos.domain.models import PrometheosError

    agent = _Agent(args)
    cid = agent.bound_claim(Path(args.folder), args.name)
    corr = new_id("correction")
    agent.create("correction", corr, {
        "target_refs": [{"id": cid, "rev": 1}], "replacement_refs": [],
        "reason": "The stored cycle applied the steepest grade everywhere.", "evidence": [],
        "effects": [{"target_ref": {"id": cid, "rev": 1}, "effect": "withdraw",
                     "replacement_ref": None, "replacement_text": None}],
        "status": "proposed", "applied_seq": None,
    })  # fmt: skip
    words = {"what": "Withdraw the uphill-energy claim (synthetic).",
             "why": "The stored cycle used the steepest grade everywhere.",
             "recommendation": "Apply the correction.", "default_text": DEFAULT_TEXT,
             "default_at": None}  # fmt: skip
    raw = agent.request([{"op": "correction_apply", "id": corr, "base_rev": 1}], words)
    try:
        agent.svc.apply(agent.pid, raw, agent.agent)
    except PrometheosError as asked:
        if asked.code != "approval_required":
            raise
        return {"claim_id": cid, "approval_request_id": asked.details["approval_request_id"],
                "card": words}  # fmt: skip
    raise SystemExit("the card request did not wait for the owner")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "relations", "rerun", "card"])
    parser.add_argument("--home", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", default="map.link_claim_question")
    parser.add_argument("--claim", action="append", default=None)
    parser.add_argument("--reply", action="append", default=[], help="SUBJECT=JSON or *=JSON")
    parser.add_argument("--folder", default=None, help="the research root's folder")
    parser.add_argument("--name", default="run.log")
    args = parser.parse_args()
    commands = {"run": run, "relations": relations, "rerun": rerun, "card": card}
    print(json.dumps(commands[args.command](args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
