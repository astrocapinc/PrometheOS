"""Recipe runs (`kind=run`) and project package records (`kind=package`), spec/03 and spec/07.

A run records host-reported progress through a recipe from an installed package. The
host agent executes the steps; PrometheOS validates each submitted step revision
against the recipe (required predecessors, checks, skip permission, artifact
visibility) and commits it through the shared transaction engine. Completion needs
every step done or skipped with a reason, every check of a done step passing, and
outputs that match the recipe's output schema. Nothing here runs package content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from prometheos.app.apply import check_evidence
from prometheos.app.reads import Caller
from prometheos.domain.bodies import PackageBody, RunBody, RunStep
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError
from prometheos.packages import Catalog, Recipe, get_recipe, validate_against
from prometheos.store.project import ProjectStore, Txn

TERMINAL = ("completed", "cancelled")


def _write(txn: Txn, record_id: str, kind: str, rev: int, body: dict[str, Any], op: str) -> None:
    body_id = txn.insert_body(body)
    txn.write_revision(record_id, kind, rev, body_id, "normal", "active", None, op, body=body)
    txn.result(record_id, rev)


# ----- package records ------------------------------------------------------------------


def record_package(
    store: ProjectStore,
    entry: dict[str, Any],
    lock_path: Path,
    status: str,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    """Create or revise the project's record of an installed (or removed) package."""
    body = PackageBody.model_validate(
        {
            "name": entry["name"],
            "version": entry["version"],
            "origin": {k: v for k, v in entry["source"].items() if k != "path"},
            "digest": entry["package_digest"],
            "license_expression": entry["license_expression"],
            "lock_path": str(lock_path),
            "status": status,
        }
    ).model_dump(mode="json")

    def handler(txn: Txn) -> None:
        row = txn.conn.execute(
            "SELECT r.id FROM records r JOIN revisions v ON v.record_id = r.id "
            "AND v.rev = r.current_rev JOIN bodies b ON b.id = v.body_id "
            "WHERE r.kind = 'package' AND json_extract(b.encoded, '$.name') = ? "
            "AND json_extract(b.encoded, '$.lock_path') = ?",
            (body["name"], body["lock_path"]),
        ).fetchone()
        if row is None:
            _write(txn, new_id("package"), "package", 1, body, "package_record")
            return
        state = txn.get(row["id"])
        assert state is not None
        if state.body == body:
            txn.result(state.id, state.rev)
            return
        _write(txn, state.id, "package", state.rev + 1, body, "package_record")

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256({"op": "package_record", "body": body}),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=0,
    )


# ----- runs -------------------------------------------------------------------------------


def _package_for_run(txn: Txn, catalog: Catalog, package_id: str) -> dict[str, Any]:
    package = txn.get(package_id)
    if package is None or package.kind != "package":
        raise PrometheosError("not_found", "package record not found", {"id": package_id})
    if package.body["status"] != "installed":
        raise PrometheosError(
            "policy_denied", f"package is {package.body['status']} in this project", {}
        )
    entry = catalog.find_digest(package.body["digest"])
    if entry is None or entry["status"] != "available":
        raise PrometheosError(
            "policy_denied",
            "package is not available in the local catalog (revoked, quarantined or missing)",
            {"digest": package.body["digest"]},
        )
    return {"record": package, "entry": entry}


def _recipe(catalog: Catalog, entry: dict[str, Any], recipe_id: str) -> Recipe:
    return get_recipe(catalog, f"{entry['name']}@{entry['version']}", recipe_id)[1]


def start(
    store: ProjectStore,
    catalog: Catalog,
    *,
    package_id: str,
    recipe_id: str,
    inputs: Any,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    def handler(txn: Txn) -> None:
        found = _package_for_run(txn, catalog, package_id)
        recipe = _recipe(catalog, found["entry"], recipe_id)
        validate_against(recipe.inputs, inputs, "inputs")
        if not isinstance(inputs, dict):
            raise PrometheosError("validation_error", "recipe inputs must be a JSON object", {})
        body = RunBody.model_validate(
            {
                "package_ref": {"id": package_id, "rev": found["record"].rev},
                "package_digest": found["entry"]["package_digest"],
                "recipe_id": recipe_id,
                "inputs": inputs,
                "input_digest": canonical_sha256(inputs),
                "steps": [
                    {
                        "id": s.id,
                        "status": "pending",
                        "artifact_refs": [],
                        "check_results": [],
                        "reason": None,
                    }
                    for s in recipe.steps
                ],
                "status": "planned",
                "outputs": None,
            }
        ).model_dump(mode="json")
        _write(txn, new_id("run"), "run", 1, body, "workflow_start")

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {
                "op": "workflow_start",
                "package_id": package_id,
                "recipe": recipe_id,
                "inputs": inputs,
            }
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=0,
    )


def _check_ids(recipe: Recipe, step_id: str) -> list[str]:
    step = next(s for s in recipe.steps if s.id == step_id)
    return [f"{step_id}.{n}" for n in range(1, len(step.checks) + 1)]


def _run_state(
    txn: Txn, catalog: Catalog, run_id: str, base_rev: int, *, need_recipe: bool = True
) -> tuple[Any, Recipe | None]:
    run = txn.get(run_id)
    if run is None or run.kind != "run":
        raise PrometheosError("not_found", "run not found", {"id": run_id})
    if run.rev != base_rev:
        raise PrometheosError(
            "revision_conflict", "run changed; read it again", {"id": run_id, "rev": run.rev}
        )
    if run.body["status"] in TERMINAL:
        raise PrometheosError("policy_denied", f"run is {run.body['status']}", {"id": run_id})
    if not need_recipe:
        return run, None
    found = _package_for_run(txn, catalog, run.body["package_ref"]["id"])
    if found["entry"]["package_digest"] != run.body["package_digest"]:
        raise PrometheosError(
            "stale_task", "the installed package changed since this run started", {}
        )
    return run, _recipe(catalog, found["entry"], run.body["recipe_id"])


def _overall(steps: list[dict[str, Any]]) -> str:
    if any(s["status"] == "blocked" for s in steps):
        return "blocked"
    if all(s["status"] == "pending" for s in steps):
        return "planned"
    return "active"


def step(
    store: ProjectStore,
    catalog: Catalog,
    *,
    run_id: str,
    base_rev: int,
    step_id: str,
    status: str,
    artifact_refs: list[dict[str, Any]],
    check_results: list[dict[str, Any]],
    reason: str | None,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    submitted = {
        "id": step_id,
        "status": status,
        "artifact_refs": artifact_refs,
        "check_results": check_results,
        "reason": reason,
    }

    def handler(txn: Txn) -> None:
        run, found_recipe = _run_state(txn, catalog, run_id, base_rev)
        assert found_recipe is not None
        recipe = found_recipe
        try:
            parsed = RunStep.model_validate_json(json.dumps(submitted))
        except ValidationError as exc:
            raise PrometheosError(
                "validation_error", "step revision does not match the run schema",
                {"errors": str(exc)[:1000]},
            ) from exc  # fmt: skip
        definition = next((s for s in recipe.steps if s.id == step_id), None)
        if definition is None:
            raise PrometheosError("not_found", "step not in this recipe", {"step": step_id})
        steps = [dict(s) for s in run.body["steps"]]
        by_id = {s["id"]: s for s in steps}
        if status in ("in_progress", "done"):
            unmet = [
                r for r in definition.requires if by_id[r]["status"] not in ("done", "skipped")
            ]
            if unmet:
                raise PrometheosError(
                    "policy_denied", "required steps are not finished", {"requires": unmet}
                )
        if by_id[step_id]["status"] in ("done", "skipped") and status not in ("done", "skipped"):
            moved = [
                s.id
                for s in recipe.steps
                if step_id in s.requires and by_id[s.id]["status"] != "pending"
            ]
            if moved:
                raise PrometheosError(
                    "policy_denied", "dependent steps already started", {"dependents": moved}
                )
        if status in ("blocked", "skipped") and not reason:
            raise PrometheosError("validation_error", f"a {status} step needs a reason", {})
        if status == "skipped" and not definition.skippable:
            raise PrometheosError(
                "policy_denied", "the recipe does not allow skipping this step", {}
            )
        expected_checks = _check_ids(recipe, step_id)
        given = [c["id"] for c in check_results]
        if len(given) != len(set(given)) or not set(given) <= set(expected_checks):
            raise PrometheosError(
                "validation_error",
                "check results must name this step's checks once",
                {"checks": expected_checks},
            )
        if status == "done":
            passing = {c["id"] for c in check_results if c["result"] == "pass"}
            missing = [c for c in expected_checks if c not in passing]
            if missing:
                raise PrometheosError(
                    "policy_denied", "a done step needs every check to pass", {"checks": missing}
                )
        for ref in parsed.artifact_refs:
            state = txn.get(ref.id)
            if state is None or not txn.rev_exists(ref.id, ref.rev):
                raise PrometheosError("not_found", "artifact record not found", {"id": ref.id})
            if state.kind in ("run", "package", "task", "project") or ref.id == run_id:
                raise PrometheosError(
                    "validation_error", f"a {state.kind} record is not a research artifact",
                    {"id": ref.id},
                )  # fmt: skip
        for result in parsed.check_results:
            for evidence in result.evidence:
                check_evidence(txn, store.directory, evidence)
        by_id[step_id].update(submitted)
        body = RunBody.model_validate(
            {**run.body, "steps": steps, "status": _overall(steps)}
        ).model_dump(mode="json")
        _write(txn, run_id, "run", run.rev + 1, body, "workflow_step")

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {"op": "workflow_step", "run": run_id, "base_rev": base_rev, "step": submitted}
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=0,
    )


def finish(
    store: ProjectStore,
    catalog: Catalog,
    *,
    run_id: str,
    base_rev: int,
    outcome: str,
    outputs: Any,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    """Complete (validated outputs, no unresolved step) or cancel a run."""
    if outcome not in TERMINAL:
        raise PrometheosError("validation_error", "outcome must be completed or cancelled", {})

    def handler(txn: Txn) -> None:
        run, recipe = _run_state(txn, catalog, run_id, base_rev, need_recipe=outcome == "completed")
        new_outputs = None
        if outcome == "completed":
            assert recipe is not None
            open_steps = [
                s["id"] for s in run.body["steps"] if s["status"] not in ("done", "skipped")
            ]
            if open_steps:
                raise PrometheosError(
                    "policy_denied", "unresolved steps remain", {"steps": open_steps}
                )
            validate_against(recipe.outputs, outputs, "outputs")
            if not isinstance(outputs, dict):
                raise PrometheosError("validation_error", "recipe outputs must be an object", {})
            new_outputs = outputs
        body = RunBody.model_validate(
            {**run.body, "status": outcome, "outputs": new_outputs}
        ).model_dump(mode="json")
        _write(txn, run_id, "run", run.rev + 1, body, f"workflow_{outcome}")

    return store.commit(
        request_id=request_id,
        request_sha256=canonical_sha256(
            {
                "op": "workflow_finish",
                "run": run_id,
                "base_rev": base_rev,
                "outcome": outcome,
                "outputs": outputs,
            }
        ),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=0,
    )


def status(
    conn: Any, store_dir: Path, catalog: Catalog, run_id: str, caller: Caller
) -> dict[str, Any]:
    from prometheos.app.reads import get_record

    run = get_record(conn, store_dir, caller, run_id)
    if run["kind"] != "run":
        raise PrometheosError("not_found", "run not found", {"id": run_id})
    body = run["body"]
    entry = catalog.find_digest(body["package_digest"])
    recipe = None
    if entry is not None:
        recipe = next(
            (r for r in catalog.sidecar(entry).recipes if r.id == body["recipe_id"]), None
        )
    lines = [f"Run {run_id} ({body['status']}) recipe {body['recipe_id']}"]
    for s in body["steps"]:
        lines.append(f"- [{s['status']}] {s['id']}" + (f": {s['reason']}" if s["reason"] else ""))
    next_steps = []
    if recipe is not None and body["status"] not in TERMINAL:
        done = {s["id"] for s in body["steps"] if s["status"] in ("done", "skipped")}
        for definition in recipe.steps:
            current = next(s for s in body["steps"] if s["id"] == definition.id)
            if current["status"] in ("pending", "in_progress") and set(definition.requires) <= done:
                next_steps.append(definition.id)
    return {
        "run": run,
        "recipe": recipe.model_dump(mode="json") if recipe else None,
        "ready_steps": next_steps,
        "package_in_catalog": entry is not None,
        "text": "\n".join(lines),
    }
