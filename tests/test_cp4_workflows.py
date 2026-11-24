"""Recipe rendering and run records (C04-PACKAGE): host-reported progress validated
against the recipe, committed through the shared transaction engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, OWNER_ACT, Env, create_entry
from cp4_support import import_package, install, write_package

from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _installed(env: Env, tmp_path: Path, version: str = "1.0.0") -> str:
    import_package(env.svc, write_package(tmp_path / f"src-{version}", version=version))
    target = tmp_path / "proj"
    target.mkdir(exist_ok=True)
    result = install(
        env.svc, f"synthetic-evidence@{version}", "claude-code", target, env.project_id
    )
    return str(result["record"]["results"][0]["id"])


def _start(env: Env, package_id: str, inputs: Any, rid: str | None = None) -> dict[str, Any]:
    return env.svc.workflow_start(
        env.project_id,
        package_id=package_id,
        recipe_id="evidence-pass",
        inputs=inputs,
        request_id=rid or new_request_id(),
    )


def _step(env: Env, run_id: str, step_id: str, status: str, **kw: Any) -> dict[str, Any]:
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    return env.svc.workflow_step(
        env.project_id,
        run_id=run_id,
        base_rev=kw.pop("base_rev", run["rev"]),
        step_id=step_id,
        status=status,
        artifact_refs=kw.pop("artifact_refs", []),
        check_results=kw.pop("check_results", []),
        reason=kw.pop("reason", None),
        request_id=kw.pop("request_id", new_request_id()),
    )


def _passes(step: str, n: int) -> list[dict[str, Any]]:
    return [{"id": f"{step}.{i}", "result": "pass", "evidence": []} for i in range(1, n + 1)]


def test_render_validates_inputs_and_disclaims_execution(env: Env, tmp_path: Path) -> None:
    _installed(env, tmp_path)
    rendered = env.svc.workflow_render("synthetic-evidence", "evidence-pass", {"question": "Why?"})
    assert "PrometheOS records progress; it does not run them" in rendered["text"]
    assert "## 3. record" in rendered["text"] and "Requires: gather" in rendered["text"]
    with pytest.raises(PrometheosError) as exc:
        env.svc.workflow_render("synthetic-evidence", "evidence-pass", {"question": 4})
    assert exc.value.code == "validation_error"


def test_run_lifecycle_enforces_order_checks_skip_and_outputs(env: Env, tmp_path: Path) -> None:
    package_id = _installed(env, tmp_path)
    with pytest.raises(PrometheosError):
        _start(env, package_id, {"question": "no", "extra": 1})
    rid = new_request_id()
    started = _start(env, package_id, {"question": "Does the plant hold?"}, rid)
    assert _start(env, package_id, {"question": "Does the plant hold?"}, rid)["replayed"] is True
    run_id = started["results"][0]["id"]
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    assert run["body"]["status"] == "planned"
    assert [s["status"] for s in run["body"]["steps"]] == ["pending"] * 3
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "record", "in_progress")
    assert exc.value.details["requires"] == ["gather"]
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "gather", "done")  # its check has not passed
    assert exc.value.code == "policy_denied"
    with pytest.raises(PrometheosError):
        _step(env, run_id, "gather", "skipped", reason="no time")  # not skippable
    with pytest.raises(PrometheosError):
        _step(env, run_id, "optional-plot", "skipped")  # needs a reason
    _step(env, run_id, "optional-plot", "skipped", reason="no plotting needed")
    _step(env, run_id, "gather", "done", check_results=_passes("gather", 1))
    note = new_id("entry")
    env.apply([create_entry(note)])
    with pytest.raises(PrometheosError):
        _step(env, run_id, "record", "done", check_results=[
            {"id": "record.1", "result": "pass", "evidence": []},
            {"id": "record.2", "result": "fail", "evidence": []},
        ])  # fmt: skip
    _step(
        env, run_id, "record", "done",
        artifact_refs=[{"id": note, "rev": 1}], check_results=_passes("record", 2),
    )  # fmt: skip
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    assert run["body"]["status"] == "active"
    with pytest.raises(PrometheosError):
        env.svc.workflow_finish(
            env.project_id, run_id=run_id, base_rev=run["rev"], outcome="completed",
            outputs={"wrong": True}, request_id=new_request_id(),
        )  # fmt: skip
    env.svc.workflow_finish(
        env.project_id, run_id=run_id, base_rev=run["rev"], outcome="completed",
        outputs={"entry_id": note}, request_id=new_request_id(),
    )  # fmt: skip
    done = env.svc.get(env.project_id, OWNER, run_id, None)
    assert done["body"]["status"] == "completed" and done["body"]["outputs"] == {"entry_id": note}
    with pytest.raises(PrometheosError):
        _step(env, run_id, "record", "in_progress")  # completed runs are closed
    status = env.svc.workflow_status(env.project_id, run_id, AGENT)
    assert "[skipped] optional-plot: no plotting needed" in status["text"]


def test_completion_needs_every_step_resolved(env: Env, tmp_path: Path) -> None:
    run_id = _start(env, _installed(env, tmp_path), {"question": "Ready?"})["results"][0]["id"]
    _step(env, run_id, "gather", "done", check_results=_passes("gather", 1))
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    with pytest.raises(PrometheosError) as exc:
        env.svc.workflow_finish(
            env.project_id, run_id=run_id, base_rev=run["rev"], outcome="completed",
            outputs={"entry_id": "x"}, request_id=new_request_id(),
        )  # fmt: skip
    assert exc.value.details["steps"] == ["optional-plot", "record"]
    assert env.svc.workflow_status(env.project_id, run_id)["ready_steps"] == [
        "optional-plot", "record",
    ]  # fmt: skip


def test_stale_base_blocked_status_and_hidden_artifacts(env: Env, tmp_path: Path) -> None:
    run_id = _start(env, _installed(env, tmp_path), {"question": "Which scope?"})["results"][0][
        "id"
    ]
    _step(env, run_id, "gather", "blocked", reason="source not yet available")
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    assert run["body"]["status"] == "blocked"
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "gather", "in_progress", base_rev=1)
    assert exc.value.code == "revision_conflict"
    hidden = new_id("entry")
    env.apply([create_entry(hidden, sensitivity="restricted")], caller=OWNER)
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "gather", "in_progress", artifact_refs=[{"id": hidden, "rev": 1}])
    assert exc.value.code == "not_found"  # agent-scope writes cannot see or cite it


def test_runs_change_only_through_workflow_commands(env: Env, tmp_path: Path) -> None:
    package_id = _installed(env, tmp_path)
    run_id = _start(env, package_id, {"question": "Which scope?"})["results"][0]["id"]
    run = env.svc.get(env.project_id, OWNER, run_id, None)
    forged = {**run["body"], "status": "completed", "outputs": {"entry_id": "fake"}}
    for op in (
        {"op": "revise", "id": run_id, "base_rev": run["rev"], "sensitivity": "normal",
         "occurred_at": None, "body": forged},
        {"op": "create", "kind": "run", "id": new_id("run"), "base_rev": 0,
         "sensitivity": "normal", "occurred_at": None, "body": forged},
        {"op": "archive", "id": package_id, "base_rev": 1},
    ):  # fmt: skip
        with pytest.raises(PrometheosError) as exc:
            env.apply([op], caller=OWNER)
        assert exc.value.code == "policy_denied"


def test_updated_or_revoked_package_stops_existing_runs(env: Env, tmp_path: Path) -> None:
    package_id = _installed(env, tmp_path)
    run_id = _start(env, package_id, {"question": "Which scope?"})["results"][0]["id"]
    env.svc.skill_revoke("synthetic-evidence@1.0.0")
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "gather", "in_progress")
    assert exc.value.code == "policy_denied"
    with pytest.raises(PrometheosError):
        _start(env, package_id, {"question": "Which scope?"})
    env.svc.skill_trust("synthetic-evidence@1.0.0", authority=OWNER_ACT)
    _installed(env, tmp_path, version="1.1.0")  # update the projection
    with pytest.raises(PrometheosError) as exc:
        _step(env, run_id, "gather", "in_progress")
    assert exc.value.code in ("stale_task", "policy_denied")
