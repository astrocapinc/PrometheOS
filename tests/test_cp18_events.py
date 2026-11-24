"""CP18.2 research events (A05; C18-EVENTS; decision 0035).

- The catalogue is a closed contract: each type fixes its subject kind, a closed data object
  with no free text, and its emitter; the per-type schemas are pinned append-only.
- Every operation the catalogue lists as an emitter writes its event in the same transaction
  as its change: one scenario per type checks the event, a mutation (emission disabled) makes
  every check fail, and a failing emission rolls the change back.
- Detectors fire once per condition, across restarts; nothing they write starts anything.
- Acknowledgement is the owner's; an agent's request is an approval. Purge removes the events
  of purged records with them, and nothing else deletes an event.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from cp1_support import AGENT, FULL, OWNER, OWNER_ACT, Env
from cp7_support import create
from cp10_continuity_support import Scenario
from test_cp18_work_items import add_work_item, revise, transition, work_body

from prometheos.app import events as events_mod
from prometheos.app import outbox
from prometheos.app.service import Service
from prometheos.domain import events as catalogue
from prometheos.domain.bodies import ActionBody
from prometheos.domain.canonical import dumps_canonical
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError

PAST = "2000-01-01T00:00:00.000000Z"


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def stored(store_dir: Path, event_type: str | None = None) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(store_dir / "record.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events ORDER BY occurred_at, seq").fetchall()
    found = []
    for row in rows:
        item = dict(row)
        item["subject_ref"] = json.loads(item["subject_ref"])
        item["data"] = json.loads(item["data"])
        if event_type is None or item["type"] == event_type:
            found.append(item)
    return found


def store_digest(store_dir: Path) -> str:
    """The logical content of the whole store (every table, every row)."""
    path = (store_dir / "record.sqlite").as_posix()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()


def sql(store_dir: Path, statement: str, params: tuple[Any, ...] = ()) -> None:
    with closing(sqlite3.connect(store_dir / "record.sqlite")) as conn:
        conn.execute(statement, params)
        conn.commit()


def refused(fn: Callable[[], Any]) -> PrometheosError:
    with pytest.raises(PrometheosError) as err:
        fn()
    return err.value


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ----- the catalogue contract ----------------------------------------------------------------


def test_action_field_names_match_the_action_body() -> None:
    # CP20.3 (decision 0084): the catalogue is append-only, so work_item.changed does not name
    # the later ``verification`` field; a planned check's changes show in the verification plan.
    fields = [f for f in ActionBody.model_fields if f != "verification"]
    assert list(get_args(catalogue.ActionField)) == fields


def test_every_type_has_a_subject_kind_closed_data_and_an_emitter() -> None:
    assert len(catalogue.CATALOGUE) == len(catalogue.EVENT_MODELS) == 11
    for name, entry in catalogue.CATALOGUE.items():
        schema = entry.model.model_json_schema()
        assert schema["x-emitter"] == entry.emitter and entry.emitter, name
        assert entry.subject_kinds, name
        data = entry.model.model_fields["data"].annotation
        assert data is not None and data.model_config.get("extra") == "forbid", name


def _strings(schema: dict[str, Any], defs: dict[str, Any], where: str) -> list[str]:
    """Every string-typed leaf of a JSON Schema that is not pinned by a pattern, an enum, a
    const or a property-name enum: the shapes free text could take."""
    loose: list[str] = []
    if "$ref" in schema:
        return _strings(defs[schema["$ref"].rsplit("/", 1)[1]], defs, where)
    for key in ("anyOf", "oneOf", "allOf"):
        for i, part in enumerate(schema.get(key, [])):
            loose += _strings(part, defs, f"{where}[{i}]")
    if schema.get("type") == "string" and not ({"pattern", "enum", "const"} & set(schema)):
        loose.append(where)
    for name, prop in schema.get("properties", {}).items():
        loose += _strings(prop, defs, f"{where}.{name}")
    if isinstance(schema.get("items"), dict):
        loose += _strings(schema["items"], defs, f"{where}[]")
    if isinstance(schema.get("additionalProperties"), dict):
        loose += _strings(schema["additionalProperties"], defs, f"{where}{{}}")
        names = schema.get("propertyNames", {})
        if not ({"pattern", "enum", "const"} & set(names)):
            loose.append(f"{where} keys")
    return loose


def test_event_data_and_subjects_carry_no_free_text_or_path() -> None:
    for name, entry in catalogue.CATALOGUE.items():
        for field in ("data", "subject_ref"):
            model = entry.model.model_fields[field].annotation
            assert model is not None
            schema = model.model_json_schema()
            assert _strings(schema, schema.get("$defs", {}), f"{name}.{field}") == [], name


# A type is never redefined in place (spec/04): each registered type's schema is pinned here.
# Adding a type appends a pin; changing or removing a pinned type fails this test.
PINNED_TYPES = {
    "work_item.created": ("bf7fee86f8a57dd2004f5e2f56fd88bb754046f7cb9e406e325596e003032d72"),
    "work_item.changed": ("b55ff36283fe8098755ab7bd6daab014de07d0e86a0d491e9666a2ff97ee9b36"),
    "deadline.approaching": ("d8d87fc732efeb1e782911a08b24a9b5b61fd8d1317383bc7e521fb5d88bf70a"),
    "approval.requested": ("664d73c1673c822bfdddfd45115c93ebcdcf9577642296f31da6303a177c8b51"),
    "approval.decided": ("69df88296bee19d5b1a314ae79bfb2978d3e59d3e2cf5b89bf030ab245839ae3"),
    "result.awaiting_interpretation": (
        "56c27a8cba744ae85ddc13eba365bf0b5a957cc7db10dbfa3a46aff5441b87ff"
    ),
    "correction.flagged_dependents": (
        "06766257d08e2e8910c43ca4323c70e8b94193612c5b76afb87b22070fbc5103"
    ),
    "backup.coverage_gap": ("04bb1e7a813c8f6408e29412d55d13364a81564ef542e73e2753912066ba912d"),
    "source.unavailable": ("396bdb9d7683ff1fc49c3dfd8c464760531ccc51d1b914f438c859e687ac8b0e"),
    "run.outcome_unknown": ("5e8ec973f5918bef77a1628a22542dc0c90404387a1eb08fadd26671ee062b7c"),
    # Decision 0108 (CP20 gap 12): appended, detector-emitted.
    "claim.needs_review": ("a025f8e0a32a218ebcbf4d680b77f6801075069c6d8a3511348616bc52c77e25"),
}


def type_digest(model: type[Any]) -> str:
    return hashlib.sha256(dumps_canonical(model.model_json_schema())).hexdigest()


def test_the_catalogue_is_append_only() -> None:
    names = list(catalogue.CATALOGUE)
    assert names[: len(PINNED_TYPES)] == list(PINNED_TYPES), "types are appended, never removed"
    assert set(PINNED_TYPES) == set(names), "a new type is pinned here too (review s2-a F6)"
    for name, pin in PINNED_TYPES.items():
        digest = type_digest(catalogue.CATALOGUE[name].model)
        assert digest == pin, f"{name} was redefined (schema digest {digest})"


# ----- same-transaction emission (one scenario per emitting type) --------------------------


@dataclass
class Case:
    store_dir: Path
    event_type: str
    subject_id: str
    seq: int  # the project sequence the event must carry
    data: dict[str, Any]


def _created(tmp: Path) -> Case:
    env = Env(tmp)
    action_id = new_id("action")
    result = env.apply([create("action", work_body(), action_id)], caller=AGENT)
    data = {"status": "proposed", "due_at": work_body()["due_at"]}
    return Case(env.store_dir, "work_item.created", action_id, result["project_seq"], data)


def _changed(tmp: Path) -> Case:
    env = Env(tmp)
    action_id = add_work_item(env)
    result = env.apply([transition(action_id, 2, "in_progress")], caller=AGENT)
    data = {"changed": ["status"], "status": "in_progress", "due_at": work_body()["due_at"]}
    return Case(env.store_dir, "work_item.changed", action_id, result["project_seq"], data)


def _requested(env: Env) -> str:
    action_id = new_id("action")
    denied = refused(
        lambda: env.apply([create("action", work_body(), action_id), {
            "op": "transition", "id": action_id, "base_rev": 1, "to": "ready",
            "reason": "Agent asks", "evidence": []}], caller=AGENT)
    )  # fmt: skip
    assert denied.code == "approval_required"
    return str(denied.details["approval_request_id"])


def _approval_requested(tmp: Path) -> Case:
    env = Env(tmp)
    approval_id = _requested(env)
    data = {"operation": "apply", "scope": "exact_request"}
    return Case(env.store_dir, "approval.requested", approval_id, env.seq(), data)


def _approval_decided(tmp: Path) -> Case:
    env = Env(tmp)
    approval_id = _requested(env)
    plan = env.svc.approval_get(env.project_id, AGENT, approval_id)["plan_sha256"]
    env.svc.approval_decide(
        env.project_id, approval_id, grant=False, plan_sha256=plan, owner=OWNER_ACT
    )
    return Case(env.store_dir, "approval.decided", approval_id, env.seq(), {"decision": "denied"})


def _flagged(tmp: Path) -> Case:
    s = Scenario(tmp).build()
    apply_op, correction_id = s.supersede_correction()
    result = s.env.apply_protected([apply_op], caller=OWNER)
    data = {"dependents": {"action": 1, "anchor": 2, "claim": 1}, "total": 4}
    return Case(
        s.env.store_dir, "correction.flagged_dependents", correction_id, result["project_seq"], data
    )


def _unknown_outcome(tmp: Path) -> Case:
    env = Env(tmp)
    view = env.svc.import_plan(
        env.project_id, AGENT, mode="none", root_alias="research", paths=["baseline.md"]
    )
    run_id = str(view["run_id"])
    call = json.dumps(
        {"provider": "test", "external_ref": None, "request_sha256": None, "started_at": PAST}
    )
    sql(env.store_dir, "UPDATE operation_runs SET state = 'running', lease_owner = 'gone', "
        "lease_expires_at = ?, external_effect = 'started', external_call = ?, stage = 'scan' "
        "WHERE id = ?", (PAST, call, run_id))  # fmt: skip
    env.svc.run_recover(env.project_id)
    data = {"operation": "source_import_plan", "stage": "scan"}
    return Case(env.store_dir, "run.outcome_unknown", run_id, env.seq(), data)


def _changed_by_revise(tmp: Path) -> Case:
    env = Env(tmp)
    action_id = add_work_item(env)
    body = env.svc.get(env.project_id, OWNER, action_id, None)["body"]
    result = revise(env, action_id, 2, body | {"labels": ["revised"]})
    data = {"changed": ["labels"], "status": "ready", "due_at": work_body()["due_at"]}
    return Case(env.store_dir, "work_item.changed", action_id, result["project_seq"], data)


def _provider_unknown() -> tuple[Any, Any]:
    from provider_support import ProviderEnv

    from prometheos.providers import transport as tp

    return ProviderEnv, tp.OutcomeUnknown("TimeoutError")


def _unknown_at_finish(tmp: Path) -> Case:
    """The worker's own finish (review s2-a F1): a provider call whose outcome is unknown."""
    provider_env, outcome = _provider_unknown()
    env = provider_env(tmp)
    env.configure()
    env.script(outcome)
    run_id = refused(env.run).details["run_id"]
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        operation, stage = conn.execute(
            "SELECT operation, stage FROM operation_runs WHERE id = ?", (run_id,)
        ).fetchone()
    data = {"operation": operation, "stage": (stage or "start").replace(" ", "_")}
    return Case(env.store_dir, "run.outcome_unknown", run_id, env.seq(), data)


# One scenario per emitting site: "<type>" or "<type> (<path>)".
SCENARIOS: dict[str, Callable[[Path], Case]] = {
    "work_item.created": _created,
    "work_item.changed": _changed,
    "work_item.changed (revise)": _changed_by_revise,
    "approval.requested": _approval_requested,
    "approval.decided": _approval_decided,
    "correction.flagged_dependents": _flagged,
    "run.outcome_unknown": _unknown_outcome,
    "run.outcome_unknown (worker finish)": _unknown_at_finish,
}


def check(case: Case) -> list[str]:
    """What is wrong with the outbox after the case's operation committed."""
    found = [
        e
        for e in stored(case.store_dir, case.event_type)
        if e["subject_ref"]["id"] == case.subject_id and e["project_seq"] == case.seq
    ]
    if len(found) != 1:
        return [f"{case.event_type}: {len(found)} events for the commit, expected 1"]
    problems = []
    if found[0]["data"] != case.data:
        problems.append(f"{case.event_type}: data {found[0]['data']} != {case.data}")
    if found[0]["acknowledged_at"] is not None:
        problems.append(f"{case.event_type}: an agent-caused event is open until acknowledged")
    return problems


def test_every_transaction_emitter_has_a_scenario() -> None:
    emitters = {n for n, e in catalogue.CATALOGUE.items() if e.in_transaction}
    assert emitters == {site.split(" ")[0] for site in SCENARIOS}


@pytest.mark.parametrize("event_type", sorted(SCENARIOS))
def test_each_emitting_operation_writes_its_event_with_its_change(
    event_type: str, tmp_path: Path
) -> None:
    assert check(SCENARIOS[event_type](tmp_path)) == []


@pytest.mark.parametrize("event_type", sorted(SCENARIOS))
def test_mutation_an_operation_committing_without_its_event_fails_the_check(
    event_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The targeted mutation (C18-EVENTS): with emission disabled every operation still
    commits its change, and the check above reports the missing event."""
    monkeypatch.setattr(outbox, "emit", lambda *a, **k: None)
    assert check(SCENARIOS[event_type](tmp_path)) != []


def _broken_emit(*_: Any, **__: Any) -> None:
    raise RuntimeError("emission failed")


def test_a_failing_emission_rolls_back_the_record_change(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = env.seq()
    action_id = new_id("action")
    monkeypatch.setattr(outbox, "emit", _broken_emit)
    with pytest.raises(RuntimeError):
        env.apply([create("action", work_body(), action_id)], caller=AGENT)
    assert env.seq() == seq and stored(env.store_dir) == []
    assert refused(lambda: env.svc.get(env.project_id, OWNER, action_id, None)).code == "not_found"


def test_a_failing_emission_rolls_back_the_approval_request(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox, "emit", _broken_emit)
    with pytest.raises(RuntimeError):
        _requested(env)
    monkeypatch.undo()
    assert env.svc.approval_list(env.project_id, AGENT)["approvals"] == []


def test_a_failing_emission_rolls_back_the_run_recovery(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    view = env.svc.import_plan(
        env.project_id, AGENT, mode="none", root_alias="research", paths=["baseline.md"]
    )
    call = json.dumps(
        {"provider": "test", "external_ref": None, "request_sha256": None, "started_at": PAST}
    )
    sql(env.store_dir, "UPDATE operation_runs SET state = 'running', lease_owner = 'gone', "
        "lease_expires_at = ?, external_effect = 'started', external_call = ? WHERE id = ?",
        (PAST, call, view["run_id"]))  # fmt: skip
    monkeypatch.setattr(outbox, "emit", _broken_emit)
    with pytest.raises(RuntimeError):
        env.svc.run_recover(env.project_id)
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        state = conn.execute(
            "SELECT state FROM operation_runs WHERE id = ?", (view["run_id"],)
        ).fetchone()[0]
    assert state == "running" and stored(env.store_dir) == []


def test_a_retried_request_emits_nothing_more(env: Env) -> None:
    action_id = new_id("action")
    rid = new_request_id()
    seq = env.seq()
    env.apply([create("action", work_body(), action_id)], expected=seq, rid=rid, caller=AGENT)
    replay = env.apply(
        [create("action", work_body(), action_id)], expected=seq, rid=rid, caller=AGENT
    )
    assert replay["replayed"] is True
    assert len(stored(env.store_dir, "work_item.created")) == 1


def test_the_owners_own_work_item_changes_are_recorded_acknowledged(env: Env) -> None:
    action_id = new_id("action")
    env.apply([create("action", work_body(), action_id)], caller=OWNER)
    (created,) = stored(env.store_dir, "work_item.created")
    assert created["acknowledged_by"] == "test" and created["acknowledged_note"] == "own change"
    assert created["acknowledged_at"] == created["occurred_at"]
    # Kept, and readable: never deleted, only not raised.
    listed = env.svc.event_list(env.project_id, OWNER, state="all")["items"]
    assert [e["event_id"] for e in listed] == [created["event_id"]]
    assert env.svc.event_list(env.project_id, OWNER)["items"] == []


def test_a_legacy_action_body_reports_only_real_changes(env: Env) -> None:
    action_id = add_work_item(env)
    legacy = {k: v for k, v in work_body().items() if k not in ("description", "labels")}
    before = outbox._action(legacy)  # the A05 defaults filled in
    assert before["description"] is None and before["labels"] == []
    stored_body = env.svc.get(env.project_id, OWNER, action_id, None)["body"]
    revise(env, action_id, 2, stored_body | {"title": "Submit P1 to journal Y"})
    last = stored(env.store_dir, "work_item.changed")[-1]
    assert last["data"]["changed"] == ["title"]


# ----- detectors ----------------------------------------------------------------------------


def detect(env: Env, moment: datetime, svc: Service | None = None) -> list[dict[str, Any]]:
    result = (svc or env.svc).events_detect(env.project_id, now=moment)
    return list(result["emitted"])


def test_deadline_notices_at_14_7_and_2_days_each_once_across_restarts(env: Env) -> None:
    due = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    action_id = add_work_item(env, due_at=iso(due))
    assert detect(env, due - timedelta(days=20)) == []
    fired = detect(env, due - timedelta(days=13))
    assert [(e["type"], e["subject_ref"]["id"]) for e in fired] == [
        ("deadline.approaching", action_id)
    ]
    assert detect(env, due - timedelta(days=13)) == []
    restarted = Service(home=env.home, actor="restarted")  # a new process sees the same state
    assert detect(env, due - timedelta(days=12), restarted) == []
    assert len(detect(env, due - timedelta(days=6), restarted)) == 1
    assert len(detect(env, due - timedelta(days=1))) == 1
    assert detect(env, due - timedelta(hours=1)) == []
    assert detect(env, due + timedelta(hours=1)) == []  # passed: shown as overdue, no notice
    notices = stored(env.store_dir, "deadline.approaching")
    assert [n["data"]["days_before"] for n in notices] == [14, 7, 2]
    assert all(n["data"]["due_at"] == iso(due) for n in notices)


def test_an_item_first_seen_one_day_out_gets_only_the_two_day_notice(env: Env) -> None:
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    action_id = add_work_item(env, due_at=iso(now + timedelta(days=1)))
    fired = detect(env, now)
    assert len(fired) == 1
    (notice,) = stored(env.store_dir, "deadline.approaching")
    assert notice["data"]["days_before"] == 2
    assert detect(env, now + timedelta(hours=12)) == []
    # A moved deadline is a new condition.
    body = env.svc.get(env.project_id, OWNER, action_id, None)["body"]
    revise(env, action_id, 2, body | {"due_at": iso(now + timedelta(days=13))})
    later = detect(env, now + timedelta(hours=13))
    assert [stored(env.store_dir, "deadline.approaching")[-1]["data"]["days_before"]] == [14]
    assert len(later) == 1


def test_closed_items_and_items_without_deadlines_get_no_notice(env: Env) -> None:
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    add_work_item(env, due_at=None)
    done = add_work_item(env, due_at=iso(now + timedelta(days=3)))
    env.apply([transition(done, 2, "cancelled")], caller=AGENT)
    assert detect(env, now) == []


def test_results_awaiting_interpretation_fire_once_per_entry(tmp_path: Path) -> None:
    s = Scenario(tmp_path).build()
    first = s.svc.events_detect(s.pid)["emitted"]
    awaiting = [e for e in first if e["type"] == "result.awaiting_interpretation"]
    assert [e["subject_ref"] for e in awaiting] == [
        {"kind": "version", "id": s.bare_receipt["version_id"]}
    ]
    assert not [
        e for e in s.svc.events_detect(s.pid)["emitted"]
        if e["type"] == "result.awaiting_interpretation"
    ]  # fmt: skip
    receipt = s.receipt("Sweep receipt")
    s.report(s.run_done, "reported_completed", [receipt["version_id"]])
    later = [
        e for e in s.svc.events_detect(s.pid)["emitted"]
        if e["type"] == "result.awaiting_interpretation"
    ]  # fmt: skip
    assert [e["subject_ref"] for e in later] == [{"kind": "execution", "id": s.run_done}]
    data = stored(s.env.store_dir, "result.awaiting_interpretation")[-1]["data"]
    assert data == {"state": "completed"}


def test_backup_gap_fires_when_it_grows_and_resets_when_covered(env: Env) -> None:
    env.import_files(["baseline.md"], retention="copy")
    first = [e for e in env.svc.events_detect(env.project_id)["emitted"]]
    assert [e["type"] for e in first] == ["backup.coverage_gap"]
    assert stored(env.store_dir, "backup.coverage_gap")[-1]["data"] == {"uncovered_versions": 1}
    assert env.svc.events_detect(env.project_id)["emitted"] == []
    env.import_files(["plant-result.md"], retention="copy")
    grown = env.svc.events_detect(env.project_id)["emitted"]
    assert [e["type"] for e in grown] == ["backup.coverage_gap"]
    assert stored(env.store_dir, "backup.coverage_gap")[-1]["data"] == {"uncovered_versions": 2}
    env.svc.backup(env.project_id, env.tmp / f"backup-{new_request_id()}.zip", FULL)
    assert env.svc.events_detect(env.project_id)["emitted"] == []
    env.import_files(["meeting.txt"], retention="copy")  # a new gap after the reset
    again = env.svc.events_detect(env.project_id)["emitted"]
    assert [e["type"] for e in again] == ["backup.coverage_gap"]
    assert stored(env.store_dir, "backup.coverage_gap")[-1]["data"] == {"uncovered_versions": 1}


def test_an_unavailable_source_fires_once_until_it_is_available_again(env: Env) -> None:
    imported = env.import_files(["sub/notes.txt"], retention="reference")
    source_id = imported["plan"]["items"][0]["source_id"]
    assert not [
        e for e in env.svc.events_detect(env.project_id)["emitted"]
        if e["type"] == "source.unavailable"
    ]  # fmt: skip
    notes = env.research / "sub" / "notes.txt"
    content = notes.read_bytes()
    notes.unlink()

    def unavailable() -> list[dict[str, Any]]:
        emitted = env.svc.events_detect(env.project_id)["emitted"]
        return [e for e in emitted if e["type"] == "source.unavailable"]

    assert [e["subject_ref"]["id"] for e in unavailable()] == [source_id]
    assert unavailable() == []
    notes.write_bytes(content)
    assert unavailable() == []  # available again: the state clears
    notes.unlink()
    assert len(unavailable()) == 1
    data = stored(env.store_dir, "source.unavailable")[-1]["data"]
    assert data == {"availability": "unavailable", "versions": 1}


def test_detectors_write_only_events_and_detector_state(env: Env) -> None:
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    add_work_item(env, due_at=iso(now + timedelta(days=2)))
    env.import_files(["baseline.md"], retention="copy")

    def others() -> dict[str, int]:
        with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN "
                    "('events', 'detector_state') AND name NOT LIKE 'search_fts%'"
                )
            ]
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
                for t in tables
            }

    before, seq = others(), env.seq()
    assert len(detect(env, now)) == 2
    assert others() == before and env.seq() == seq


def test_a_jobs_tick_runs_the_detectors(env: Env) -> None:
    now = datetime.now(UTC)
    add_work_item(env, due_at=iso(now + timedelta(days=5)))
    result = env.svc.jobs_tick(env.project_id)
    assert [e["type"] for e in result["events"]["emitted"]] == ["deadline.approaching"]


# ----- listing, acknowledgement, purge -------------------------------------------------------


def _agent_items(env: Env, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        action_id = new_id("action")
        env.apply([create("action", work_body(), action_id)], caller=AGENT)
        ids.append(action_id)
    return ids


def test_event_list_filters_and_pages(env: Env) -> None:
    ids = _agent_items(env, 3)
    listed = env.svc.event_list(env.project_id, AGENT, limit=2)
    assert len(listed["items"]) == 2 and listed["next_cursor"]
    rest = env.svc.event_list(env.project_id, AGENT, limit=2, cursor=listed["next_cursor"])
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    subjects = [e["subject_ref"]["id"] for e in listed["items"] + rest["items"]]
    assert sorted(subjects) == sorted(ids)
    one = env.svc.event_list(env.project_id, AGENT, subject_id=ids[0])["items"]
    assert [e["subject_ref"]["id"] for e in one] == [ids[0]]
    assert env.svc.event_list(env.project_id, AGENT, event_type="approval.decided")["items"] == []
    assert all(e["reason"].startswith("A work item was added") for e in one)
    bad = refused(lambda: env.svc.event_list(env.project_id, AGENT, event_type="x.y"))
    assert bad.code == "validation_error"


def test_agents_see_no_event_about_a_record_they_cannot_see(env: Env) -> None:
    hidden = new_id("action")
    env.apply([create("action", work_body(), hidden, sensitivity="restricted")], caller=OWNER)
    sql(env.store_dir, "UPDATE events SET acknowledged_at = NULL, acknowledged_by = NULL, "
        "acknowledged_note = NULL")  # as if an agent-scope caller had made it  # fmt: skip
    assert env.svc.event_list(env.project_id, AGENT)["items"] == []
    assert len(env.svc.event_list(env.project_id, OWNER)["items"]) == 1


def test_the_owner_acknowledges_and_only_the_acknowledgement_changes(env: Env) -> None:
    ids = _agent_items(env, 2)
    open_events = env.svc.event_list(env.project_id, OWNER)["items"]
    target = open_events[0]["event_id"]
    refusal = refused(lambda: env.svc.event_acknowledge(env.project_id, [target], authority=None))
    assert refusal.code == "policy_denied"
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        before = conn.execute(
            "SELECT event_id, type, subject_ref, occurred_at, project_seq, data FROM events "
            "ORDER BY event_id"
        ).fetchall()
    seq = env.seq()
    done = env.svc.event_acknowledge(
        env.project_id, [target], "Seen; nothing to do.", authority=OWNER_ACT
    )
    (acked,) = done["acknowledged"]
    assert acked["acknowledged_by"] == "test-owner"
    assert acked["acknowledged_note"] == "Seen; nothing to do."
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        after = conn.execute(
            "SELECT event_id, type, subject_ref, occurred_at, project_seq, data FROM events "
            "ORDER BY event_id"
        ).fetchall()
    assert before == after and env.seq() == seq  # no record changed, nothing executed
    remaining = env.svc.event_list(env.project_id, OWNER)["items"]
    assert [e["subject_ref"]["id"] for e in remaining] != [] and target not in {
        e["event_id"] for e in remaining
    }
    again = refused(
        lambda: env.svc.event_acknowledge(env.project_id, [target], authority=OWNER_ACT)
    )
    assert again.code == "revision_conflict"
    # All or nothing: one already-acknowledged event refuses the whole call.
    other = remaining[0]["event_id"]
    mixed = refused(
        lambda: env.svc.event_acknowledge(env.project_id, [target, other], authority=OWNER_ACT)
    )
    assert mixed.code == "revision_conflict"
    assert other in {e["event_id"] for e in env.svc.event_list(env.project_id, OWNER)["items"]}
    assert len(ids) == 2


def test_an_agent_requests_acknowledgement_and_resumes_after_the_grant(env: Env) -> None:
    _agent_items(env, 1)
    (event,) = env.svc.event_list(env.project_id, AGENT)["items"]
    asked = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "event_acknowledge",
            {"event_ids": [event["event_id"]], "note": "Agent: already in the plan."},
        )
    )  # fmt: skip
    assert asked.code == "approval_required"
    approval_id = asked.details["approval_request_id"]
    assert env.svc.event_list(env.project_id, AGENT)["items"] != []  # nothing changed yet
    requested = [
        e for e in stored(env.store_dir, "approval.requested")
        if e["subject_ref"]["id"] == approval_id
    ]  # fmt: skip
    assert requested and requested[0]["data"]["operation"] == "event_acknowledge"
    env.svc.approval_decide(
        env.project_id, approval_id, grant=True,
        plan_sha256=asked.details["plan_sha256"], owner=OWNER_ACT,
    )  # fmt: skip
    resumed = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    (acked,) = resumed["result"]["acknowledged"]
    assert acked["event_id"] == event["event_id"]
    assert acked["acknowledged_by"].startswith("test-owner via ")
    open_ids = {e["event_id"] for e in env.svc.event_list(env.project_id, AGENT)["items"]}
    assert event["event_id"] not in open_ids
    replay = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert replay["replayed"] is True


def test_purge_removes_the_events_of_purged_records_only(env: Env) -> None:
    keep, gone = _agent_items(env, 2)
    plan = env.svc.purge_plan(env.project_id, [gone], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST",
        synthetic=True,
    )  # fmt: skip
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    subjects = {e["subject_ref"]["id"] for e in stored(env.store_dir)}
    assert keep in subjects and gone not in subjects


def test_reason_texts_use_only_type_and_data() -> None:
    from test_cp18_queue_text import violations

    for name, entry in catalogue.CATALOGUE.items():
        model = entry.model
        sample = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "examples" / "event"
             / f"valid-{_EXAMPLES[name]}.json").read_text("utf-8")
        )  # fmt: skip
        text = events_mod.reason(model.model_validate(sample))
        assert text and violations(text) == [], (name, text)


_EXAMPLES = {
    "work_item.created": "work-item-created",
    "work_item.changed": "work-item-changed-own",
    "deadline.approaching": "deadline-approaching",
    "approval.requested": "approval-requested",
    "approval.decided": "approval-decided",
    "result.awaiting_interpretation": "result-awaiting-execution",
    "correction.flagged_dependents": "correction-flagged",
    "backup.coverage_gap": "backup-gap",
    "source.unavailable": "source-unavailable",
    "run.outcome_unknown": "run-outcome-unknown",
    "claim.needs_review": "claim-needs-review",
}


def test_no_event_executes_an_operation(env: Env) -> None:
    """Reading or acknowledging events and running the detectors commit nothing and change no
    record (the project sequence and every record table are unchanged)."""
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    add_work_item(env, due_at=iso(now + timedelta(days=1)))
    _agent_items(env, 1)
    seq = env.seq()
    detect(env, now)
    events = env.svc.event_list(env.project_id, OWNER)["items"]
    env.svc.work_queue(env.project_id, AGENT)
    env.svc.event_acknowledge(env.project_id, [e["event_id"] for e in events], authority=OWNER_ACT)
    assert env.seq() == seq
    statuses = {
        r["id"]: r["body"]["status"]
        for r in env.svc.list_records(env.project_id, OWNER, kind="action")["items"]
    }
    assert sorted(statuses.values()) == ["proposed", "ready"]


# ----- review s2-a: atomicity at every emitting site, stale plans, start hook, concurrency -----


def fail_on(monkeypatch: pytest.MonkeyPatch, event_type: str) -> None:
    """Make only ``event_type``'s emission fail (setup events still write)."""
    original = outbox.emit

    def emit(conn: Any, kind: str, *args: Any, **kwargs: Any) -> Any:
        if kind == event_type:
            raise RuntimeError("emission failed")
        return original(conn, kind, *args, **kwargs)

    monkeypatch.setattr(outbox, "emit", emit)


def test_a_failing_decision_event_rolls_back_the_decision(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = _requested(env)
    plan = env.svc.approval_get(env.project_id, AGENT, approval_id)["plan_sha256"]
    fail_on(monkeypatch, "approval.decided")
    with pytest.raises(RuntimeError):
        env.svc.approval_decide(
            env.project_id, approval_id, grant=True, plan_sha256=plan, owner=OWNER_ACT
        )
    monkeypatch.undo()
    view = env.svc.approval_get(env.project_id, AGENT, approval_id)
    assert view["state"] == "pending" and view["decided_at"] is None
    assert stored(env.store_dir, "approval.decided") == []
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0] == 0


@pytest.mark.parametrize("how", ["transition", "revise"])
def test_a_failing_change_event_rolls_back_the_work_item_change(
    how: str, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    action_id = add_work_item(env)
    body = env.svc.get(env.project_id, OWNER, action_id, None)["body"]
    seq, before = env.seq(), len(stored(env.store_dir, "work_item.changed"))
    fail_on(monkeypatch, "work_item.changed")
    with pytest.raises(RuntimeError):
        if how == "transition":
            env.apply([transition(action_id, 2, "in_progress")], caller=AGENT)
        else:
            revise(env, action_id, 2, body | {"labels": ["revised"]})
    monkeypatch.undo()
    assert env.seq() == seq and len(stored(env.store_dir, "work_item.changed")) == before
    assert env.svc.get(env.project_id, OWNER, action_id, None)["rev"] == 2


def test_a_failing_flag_event_rolls_back_the_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Scenario(tmp_path).build()
    apply_op, correction_id = s.supersede_correction()
    seq = s.env.seq()
    fail_on(monkeypatch, "correction.flagged_dependents")
    with pytest.raises(RuntimeError):
        s.env.apply_protected([apply_op], caller=OWNER)
    monkeypatch.undo()
    assert s.env.seq() == seq and s.flags() == []
    assert s.body(correction_id)["status"] == "proposed"


def test_a_failing_outcome_event_leaves_no_unknown_run_without_its_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_env, outcome = _provider_unknown()
    env = provider_env(tmp_path)
    env.configure()
    env.script(outcome)
    fail_on(monkeypatch, "run.outcome_unknown")
    with pytest.raises(Exception):  # noqa: B017 - the worker's own error envelope may wrap it
        env.run()
    monkeypatch.undo()
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        unknown = conn.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE state = 'unknown_external'"
        ).fetchone()[0]
    assert unknown == 0 and stored(env.store_dir, "run.outcome_unknown") == []


def test_a_stale_acknowledgement_plan_is_refused_and_the_grant_is_kept(env: Env) -> None:
    """Review s2-a F3: the owner acknowledged the event after granting the agent's request; the
    resume is refused and the one-use grant is not spent."""
    _agent_items(env, 1)
    (event,) = env.svc.event_list(env.project_id, AGENT)["items"]
    asked = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "event_acknowledge", {"event_ids": [event["event_id"]]}
        )
    )
    approval_id = asked.details["approval_request_id"]
    env.svc.approval_decide(env.project_id, approval_id, grant=True,
                            plan_sha256=asked.details["plan_sha256"], owner=OWNER_ACT)  # fmt: skip
    env.svc.event_acknowledge(env.project_id, [event["event_id"]], "Seen", authority=OWNER_ACT)
    stale = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert stale.code == "revision_conflict"
    assert env.svc.approval_get(env.project_id, AGENT, approval_id)["state"] == "granted"
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        (uses,) = conn.execute(
            "SELECT c.uses FROM capabilities c JOIN approval_requests a ON a.capability_id = c.id "
            "WHERE a.id = ?",
            (approval_id,),
        ).fetchone()
    assert uses == 0
    (kept,) = env.svc.event_list(env.project_id, OWNER, state="acknowledged")["items"]
    assert kept["acknowledged_note"] == "Seen"  # the owner's acknowledgement stands


def test_the_start_hook_never_stops_a_server(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise ValueError("unexpected")

    monkeypatch.setattr(events_mod, "detect", broken)
    assert env.svc.events_detect_at_start(env.project_id) is None
    assert "did not run (ValueError)" in capsys.readouterr().err


def test_concurrent_detector_passes_fire_once(env: Env) -> None:
    import threading

    now = datetime.now(UTC)
    add_work_item(env, due_at=iso(now + timedelta(days=5)))
    errors: list[Any] = []

    def run() -> None:
        for _ in range(40):
            try:
                env.svc.events_detect(env.project_id, now=now)
                return
            except PrometheosError as exc:
                if exc.code != "busy":
                    errors.append(exc)
                    return

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(stored(env.store_dir, "deadline.approaching")) == 1


def test_purge_removes_detector_state_and_counts_the_events(env: Env) -> None:
    now = datetime.now(UTC)
    gone = add_work_item(env, due_at=iso(now + timedelta(days=3)))
    env.svc.events_detect(env.project_id, now=now)
    plan = env.svc.purge_plan(env.project_id, [gone], OWNER)
    grant = env.svc.grant(env.project_id, operation="purge", digest=plan["plan_sha256"],
                          label="SYNTHETIC-TEST", synthetic=True)  # fmt: skip
    receipt = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)["purge_receipt"]
    assert receipt["events_removed"] == 3  # created, changed (adoption), deadline notice
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM detector_state WHERE condition_key LIKE ?", (f"{gone}|%",)
        ).fetchone()[0]
    assert left == 0 and stored(env.store_dir) == []
