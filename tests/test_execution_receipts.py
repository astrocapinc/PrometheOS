"""Execution receipts and E06 dogfood fixes (decision 0009; spec/03 "Execution receipts").

A receipt records what an agent reports it ran, pinned to stored output versions by digest;
it is never upgraded to an observation. The research MIME table and the ``sources`` context
section close dogfood findings F1 and F2.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry

from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    environment = Env(tmp_path)
    (environment.research / "model.slx").write_bytes(b"PK\x03\x04 synthetic model" * 40)
    (environment.research / "trace.csv").write_text("time_s,speed\n0,0\n0.1,9.5\n", "utf-8")
    return environment


def imported(env: Env, paths: list[str], sensitivity: str = "normal") -> dict[str, dict[str, Any]]:
    """relative path -> {source_id, version_id, sha256} for freshly imported files."""
    result = env.import_files(paths, sensitivity=sensitivity)
    return {
        item["relative_path"]: {
            "source_id": item["source_id"],
            "version_id": item["version_id"],
            "sha256": item["sha256"],
        }
        for item in result["plan"]["items"]
    }


def receipt(outputs: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "observation": "reported",
        "reporter": "test agent",
        "command": "run('build_and_run.m')",
        "working_root": "research",
        "environment": {"os": "TestOS", "tools": [{"name": "MATLAB", "version": "R2025b"}]},
        "started_at": "2026-10-05T06:36:30Z",
        "ended_at": "2026-10-05T06:36:41.5Z",
        "exit_status": 0,
        "outcome": "succeeded",
        "inputs": [],
        "outputs": outputs,
        "limitations": ["synthetic test"],
    }
    body.update(extra)
    return body


def record(env: Env, body: dict[str, Any], rid: str | None = None, title: str = "run 1") -> Any:
    return env.svc.receipt_record(
        env.project_id,
        raw=json.dumps(body).encode("utf-8"),
        title=title,
        description="",
        request_id=rid or new_request_id(),
    )


def created(commit: dict[str, Any], kind: str) -> str:
    return next(r["id"] for r in commit["results"] if r["id"].startswith(kind + "_"))


def test_receipt_pins_binary_outputs_and_is_reported_not_observed(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    outputs = [dict(files["model.slx"], role="model"), dict(files["trace.csv"], role="trace")]
    commit = record(env, receipt(outputs))
    source_id, version_id = created(commit, "source"), created(commit, "version")
    source = env.svc.get(env.project_id, OWNER, source_id, None)
    version = env.svc.get(env.project_id, OWNER, version_id, None)
    assert source["body"]["origin"] == "agent_receipt" and source["body"]["root_binding"] is None
    body = version["body"]
    assert body["payload_schema"] == "execution_receipt_v1" and body["mime"] == "application/json"
    stored = env.svc.version_text(env.project_id, OWNER, version_id)
    text = stored["text"] if isinstance(stored, dict) else stored
    payload = json.loads(text)
    assert payload["observation"] == "reported"
    assert [o["version_id"] for o in payload["outputs"]] == [
        files["model.slx"]["version_id"],
        files["trace.csv"]["version_id"],
    ]
    expected = dumps_canonical(receipt(outputs))
    assert body["sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize(
    ("mutate", "code", "fragment"),
    [
        (lambda o: o.update(sha256="0" * 64), "validation_error", "digest differs"),
        (
            lambda o: o.update(source_id="source_00000000-0000-4000-8000-000000000001"),
            "not_found",
            "source that does not exist",
        ),
        (
            lambda o: o.update(version_id="version_00000000-0000-4000-8000-000000000001"),
            "not_found",
            "version that does not exist",
        ),
    ],
)
def test_receipt_artifacts_must_match_stored_versions(
    env: Env, mutate: Any, code: str, fragment: str
) -> None:
    files = imported(env, ["model.slx"])
    output = dict(files["model.slx"], role="model")
    mutate(output)
    before = env.seq()
    with pytest.raises(PrometheosError) as err:
        record(env, receipt([output]))
    assert err.value.code == code and fragment in err.value.message
    assert env.seq() == before


def test_artifact_version_must_belong_to_its_source(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    crossed = dict(files["model.slx"], version_id=files["trace.csv"]["version_id"], role="x")
    crossed["sha256"] = files["trace.csv"]["sha256"]
    with pytest.raises(PrometheosError, match="does not belong"):
        record(env, receipt([crossed]))


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ({"observation": "observed"}, "reported, never observed"),
        ({"approved_by": "researcher"}, "does not match execution_receipt_v1"),
        ({"exit_status": 3}, "does not match execution_receipt_v1"),
        ({"ended_at": "2026-10-05T06:36:29Z"}, "does not match execution_receipt_v1"),
    ],
)
def test_invalid_or_self_upgrading_receipts_are_refused(
    env: Env, extra: dict[str, Any], fragment: str
) -> None:
    files = imported(env, ["model.slx"])
    with pytest.raises(PrometheosError, match=fragment):
        record(env, receipt([dict(files["model.slx"], role="model")], **extra))


def test_working_root_must_be_an_approved_alias(env: Env) -> None:
    with pytest.raises(PrometheosError) as err:
        record(env, receipt([], working_root="elsewhere"))
    assert err.value.code == "policy_denied"


def test_receipt_inherits_the_highest_sensitivity_and_hides_from_agents(env: Env) -> None:
    files = imported(env, ["model.slx"], sensitivity="restricted")
    # Only an owner caller can name a restricted artifact (agents read it as absent).
    commit = record_as(env, receipt([dict(files["model.slx"], role="model")]), OWNER)
    version_id = created(commit, "version")
    assert env.svc.get(env.project_id, OWNER, version_id, None)["sensitivity"] == "restricted"
    with pytest.raises(PrometheosError):
        env.svc.get(env.project_id, AGENT, version_id, None)


def test_replay_is_idempotent_and_a_changed_body_conflicts(env: Env) -> None:
    files = imported(env, ["model.slx"])
    body = receipt([dict(files["model.slx"], role="model")])
    rid = new_request_id()
    first = record(env, body, rid)
    seq = env.seq()
    again = record(env, body, rid)
    assert again["replayed"] is True and again["results"] == first["results"]
    assert env.seq() == seq
    with pytest.raises(PrometheosError) as err:
        record(env, dict(body, exit_status=None), rid)
    assert err.value.code == "request_key_conflict"


def test_entry_cites_receipt_fields_with_json_pointers(env: Env) -> None:
    files = imported(env, ["model.slx"])
    commit = record(env, receipt([dict(files["model.slx"], role="model")]))
    ref = {
        "source_id": created(commit, "source"),
        "version_id": created(commit, "version"),
        "locator": {"type": "json", "pointer": ""},
        "quote_sha256": None,
    }
    output = dict(ref, locator={"type": "json", "pointer": "/outputs/0"})
    status = dict(ref, locator={"type": "json", "pointer": "/exit_status"})
    result = env.apply([create_entry(title="Run", evidence=[ref, output, status])])
    assert result["ok"] is True
    missing = dict(ref, locator={"type": "json", "pointer": "/outputs/5"})
    with pytest.raises(PrometheosError, match="JSON pointer does not resolve"):
        env.apply([create_entry(title="Run", evidence=[missing])])
    # JSON numbers lose their written form: they are cited, never bound as quantities.
    quantity = {
        "decimal": "0",
        "unit": "",
        "source_ref": status,
        "source_pointer": "json:/exit_status",
    }
    with pytest.raises(PrometheosError, match="written form"):
        env.apply([create_entry(title="Run", evidence=[ref], quantities=[quantity])])


def test_receipt_text_is_searchable(env: Env) -> None:
    record(env, receipt([], command="run('unique_dcmotor_script.m')"))
    hits = env.svc.search(env.project_id, OWNER, query="unique_dcmotor_script")
    assert any(h["kind"] == "version" for h in hits["hits"])


def test_research_mime_table_overrides_the_platform_registry(env: Env) -> None:
    (env.research / "binary.csv").write_bytes(b"\xff\xfe\x00not text")
    plan = env.svc.import_plan(
        env.project_id,
        root_alias="research",
        paths=["trace.csv", "model.slx", "binary.csv"],
        retention="copy",
        sensitivity="normal",
    )
    mimes = {i["relative_path"]: i["mime"] for i in plan["items"]}
    assert mimes == {
        "trace.csv": "text/csv",
        "model.slx": "application/vnd.mathworks.simulink",
        "binary.csv": "application/octet-stream",
    }
    env.svc.import_commit(env.project_id, plan["plan_id"])
    version_id = next(i["version_id"] for i in plan["items"] if i["relative_path"] == "trace.csv")
    assert env.svc.get(env.project_id, OWNER, version_id, None)["body"]["payload_schema"] == "text"


def test_notebook_draft_context_lists_registered_versions_with_digests(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    imported(env, ["sub/notes.txt"], sensitivity="restricted")
    ctx = env.svc.context(env.project_id, AGENT, purpose="notebook_draft", budget=12000)
    section = next(s for s in ctx["manifest"]["sections"] if s["name"] == "sources")
    for item in files.values():
        assert item["version_id"] in section["included"]
        assert f"latest {item['version_id']} sha256 {item['sha256']}" in ctx["text"]
    assert "notes.txt" not in ctx["text"]  # restricted sources never reach agent context
    names = [s["name"] for s in ctx["manifest"]["sections"]]
    assert names.index("sources") == names.index("commitments") + 1


# ----- review 0009 regressions (H1 H2 M1 L1 L2 L3) -----------------------------------


def record_as(env: Env, body: dict[str, Any], caller: Any, rid: str | None = None) -> Any:
    return env.svc.receipt_record(
        env.project_id,
        raw=json.dumps(body).encode("utf-8"),
        title="run",
        description="",
        request_id=rid or new_request_id(),
        caller=caller,
    )


def test_agent_receipt_cannot_probe_hidden_versions(env: Env) -> None:
    files = imported(env, ["model.slx"], sensitivity="restricted")
    good = dict(files["model.slx"], role="model")
    absent = dict(
        good,
        source_id="source_00000000-0000-4000-8000-0000000000aa",
        version_id="version_00000000-0000-4000-8000-0000000000ab",
    )
    before = env.seq()
    errors = []
    for artifact in (good, dict(good, sha256="0" * 64), absent):
        with pytest.raises(PrometheosError) as err:
            record_as(env, receipt([artifact]), AGENT)  # the default for CLI and MCP
        errors.append((err.value.code, err.value.message))
    assert (
        errors[0]
        == errors[1]
        == errors[2]
        == (
            "not_found",
            "receipt names a source that does not exist",
        )
    )
    assert env.seq() == before
    owner = record_as(env, receipt([good]), OWNER)
    version_id = created(owner, "version")
    assert env.svc.get(env.project_id, OWNER, version_id, None)["sensitivity"] == "restricted"


def test_raising_an_artifact_raises_its_receipts(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    commit = record(env, receipt([dict(files["model.slx"], role="model")]))
    receipt_ids = [created(commit, "source"), created(commit, "version")]
    source = env.svc.get(env.project_id, OWNER, files["model.slx"]["source_id"], None)
    revise = {
        "op": "revise",
        "id": source["id"],
        "base_rev": source["rev"],
        "sensitivity": "restricted",
        "occurred_at": None,
        "body": source["body"],
    }
    env.apply_protected([revise], caller=OWNER)
    for rid in receipt_ids:
        assert env.svc.get(env.project_id, OWNER, rid, None)["sensitivity"] == "restricted"
        with pytest.raises(PrometheosError):
            env.svc.get(env.project_id, AGENT, rid, None)
    ctx = env.svc.context(env.project_id, AGENT, purpose="notebook_draft", budget=12000)
    assert files["model.slx"]["sha256"] not in ctx["text"]
    assert receipt_ids[1] not in ctx["text"]
    assert files["trace.csv"]["version_id"] in ctx["text"]  # unrelated source still listed


def test_purging_an_artifact_purges_its_receipts(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    model = files["model.slx"]
    commit = record(
        env,
        receipt([dict(model, role="model"), dict(files["trace.csv"], role="trace")]),
    )
    receipt_ids = {created(commit, "source"), created(commit, "version")}
    plan = env.svc.purge_plan(env.project_id, [model["source_id"]], OWNER)
    assert set(plan["closure"]) == {model["source_id"], model["version_id"], *receipt_ids}
    assert files["trace.csv"]["version_id"] not in plan["closure"]
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    # The receipt payload, bodies, search text and relations no longer hold the purged
    # identity. Only the executed purge plan keeps the digests it deleted (purge design).
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            if table in ("purge_plans",) or table.startswith("search_fts"):
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
            for column in columns:
                for needle in (model["sha256"],):
                    hits = conn.execute(
                        f"SELECT COUNT(*) FROM '{table}' WHERE CAST(\"{column}\" AS TEXT) LIKE ?",  # noqa: S608
                        (f"%{needle}%",),
                    ).fetchone()[0]
                    assert hits == 0, (table, column, needle)
        # Records keep non-content tombstones by ID; no relation survives the purge.
        purged = (model["source_id"], model["version_id"], *receipt_ids)
        marks = ",".join("?" for _ in purged)
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM relations WHERE source_id IN ({marks}) "  # noqa: S608
                f"OR target_id IN ({marks})",
                purged + purged,
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()
    assert env.svc.search(env.project_id, OWNER, "build_and_run")["hits"] == []


def test_purged_versions_read_as_absent_not_as_internal_errors(env: Env) -> None:
    files = imported(env, ["model.slx", "trace.csv"])
    model = files["model.slx"]
    plan = env.svc.purge_plan(env.project_id, [model["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    with pytest.raises(PrometheosError) as err:
        record_as(env, receipt([dict(model, role="model")]), OWNER)
    assert err.value.code == "not_found"
    ref = {
        "source_id": model["source_id"],
        "version_id": model["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(title="Cites purged", evidence=[ref])], caller=OWNER)
    assert err.value.code == "not_found"


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_schema_version_must_be_the_integer_one(env: Env, version: Any) -> None:
    with pytest.raises(PrometheosError, match="does not match execution_receipt_v1"):
        record(env, receipt([], schema_version=version))


def test_sources_section_follows_commitments_and_discloses_omissions(env: Env) -> None:
    for index in range(22):
        (env.research / f"run{index:02}.txt").write_text(f"run {index}\n", "utf-8")
    env.import_files([f"run{index:02}.txt" for index in range(22)])
    ctx = env.svc.context(env.project_id, AGENT, purpose="notebook_draft", budget=12000)
    names = [s["name"] for s in ctx["manifest"]["sections"]]
    assert names.index("commitments") < names.index("sources") < names.index("findings")
    section = next(s for s in ctx["manifest"]["sections"] if s["name"] == "sources")
    assert len(section["included"]) == 20
    assert "SOURCES: 2 older source(s) not listed" in ctx["text"]


def test_a_purged_version_under_a_live_source_reads_as_absent(env: Env) -> None:
    files = imported(env, ["model.slx"])
    model = files["model.slx"]
    plan = env.svc.purge_plan(env.project_id, [model["version_id"]], OWNER)
    assert model["source_id"] not in plan["closure"]  # the source stays live
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    with pytest.raises(PrometheosError) as err:
        record_as(env, receipt([dict(model, role="model")]), OWNER)
    assert (err.value.code, err.value.message) == (
        "not_found",
        "receipt names a version that does not exist",
    )
    ref = {
        "source_id": model["source_id"],
        "version_id": model["version_id"],
        "locator": {"type": "text", "start_line": 1, "end_line": 1},
        "quote_sha256": None,
    }
    with pytest.raises(PrometheosError) as err:
        env.apply([create_entry(title="Cites purged", evidence=[ref])], caller=OWNER)
    assert (err.value.code, err.value.message) == ("not_found", "cited version does not exist")


def test_purge_follows_cites_relations_only_from_receipts(env: Env) -> None:
    files = imported(env, ["model.slx"])
    model = files["model.slx"]
    entry = create_entry(title="Mentions nothing by ID")
    env.apply([entry])
    relation = {
        "op": "relation_add",
        "source_ref": {"id": entry["id"], "rev": 1},
        "target_ref": {"id": model["version_id"], "rev": 1},
        "relation": "cites",
    }
    env.apply([relation])
    commit = record(env, receipt([dict(model, role="model")]))
    receipt_ids = {created(commit, "source"), created(commit, "version")}
    plan = env.svc.purge_plan(env.project_id, [model["source_id"]], OWNER)
    assert receipt_ids <= set(plan["closure"])
    assert entry["id"] not in plan["closure"]  # a user relation keeps its own semantics
