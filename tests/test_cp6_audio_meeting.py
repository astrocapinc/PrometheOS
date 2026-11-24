"""C06-AUDIO (part 3): a recorded meeting through transcription, agent extraction with audio
locators, reconciliation against the transcript, correction, privacy and purge (S14, R05, R15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER, OWNER_ACT

from prometheos.audio import engines, transcribe
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError

CANARY = "ZEPHYR-CANARY-7731"


@pytest.fixture(autouse=True)
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")


@pytest.fixture
def env(tmp_path: Path) -> AudioEnv:
    return AudioEnv(tmp_path)


def meeting_from_audio(
    env: AudioEnv, sensitivity: str, script: tuple[str, ...] | None = None
) -> dict[str, Any]:
    audio = env.import_audio(sensitivity=sensitivity)
    config = env.synthetic_config() if script is None else env.synthetic_config(script=script)
    done = env.svc.transcribe_audio(env.project_id, OWNER, audio["version_id"], config)
    meeting = env.svc.capture_meeting_from_transcript(
        env.project_id,
        OWNER,
        transcript_version_id=done["result_version_id"],
        audio_version_id=audio["version_id"],
        title="Supervision call",
        held_at="2026-10-01T09:00:00Z",
    )
    view = env.svc.transcript_get(env.project_id, OWNER, done["result_version_id"])
    return {
        "audio": audio,
        "transcript": done["result_version_id"],
        "transcript_source": view["source_id"],
        "meeting": meeting["meeting_id"],
        "meeting_result": meeting,
    }


def locator(spec: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    return {
        "source_id": spec["transcript_source"],
        "version_id": spec["transcript"],
        "locator": {"type": "audio", "start_ms": start, "end_ms": end},
        "quote_sha256": None,
    }


def submit_items(
    env: AudioEnv, spec: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    task = env.svc.task_prepare(
        env.project_id,
        purpose="meeting_extract",
        meeting_id=spec["meeting"],
        request_id=new_request_id(),
    )
    result = {"purpose": "meeting_extract", "items": items}
    return env.svc.task_submit(
        env.project_id, task["task_id"], json.dumps(result).encode(), new_request_id()
    ) | {"task": task}


def item(spec: dict[str, Any], kind: str, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "type": kind,
        "text": text,
        "evidence": [locator(spec, start, end)],
        "proposed_owner": None,
        "due_at": None,
    }


def test_a_meeting_from_a_transcript_names_both_versions_and_inherits_sensitivity(
    env: AudioEnv,
) -> None:
    spec = meeting_from_audio(env, "restricted")
    body = env.svc.get(env.project_id, OWNER, spec["meeting"], None)
    assert body["sensitivity"] == "restricted"
    assert [r["id"] for r in body["body"]["source_version_refs"]] == [
        spec["transcript"],
        spec["audio"]["version_id"],
    ]
    assert body["body"]["held_at"] == "2026-10-01T09:00:00Z"
    # Hidden from agents entirely: reads, reconciliation and task preparation.
    with pytest.raises(PrometheosError) as err:
        env.svc.get(env.project_id, AGENT, spec["meeting"], None)
    assert err.value.code == "not_found"
    with pytest.raises(PrometheosError):
        env.svc.reconcile(env.project_id, AGENT, spec["meeting"])
    with pytest.raises(PrometheosError):
        env.svc.task_prepare(
            env.project_id,
            purpose="meeting_extract",
            meeting_id=spec["meeting"],
            request_id=new_request_id(),
        )
    assert env.svc.reconcile(env.project_id, OWNER, spec["meeting"])["items"] == []


def test_meeting_capture_refuses_what_the_caller_cannot_see_or_that_is_not_a_transcript(
    env: AudioEnv,
) -> None:
    spec = meeting_from_audio(env, "restricted")
    with pytest.raises(PrometheosError):
        env.svc.capture_meeting_from_transcript(
            env.project_id,
            AGENT,
            transcript_version_id=spec["transcript"],
            audio_version_id=None,
            title="x",
            held_at=None,
        )
    with pytest.raises(PrometheosError) as err:  # the audio is not a transcript
        env.svc.capture_meeting_from_transcript(
            env.project_id,
            OWNER,
            transcript_version_id=spec["audio"]["version_id"],
            audio_version_id=None,
            title="x",
            held_at=None,
        )
    assert err.value.code == "validation_error"


def test_agent_extracts_proposals_from_the_transcript_with_audio_locators(env: AudioEnv) -> None:
    spec = meeting_from_audio(env, "normal")
    task = env.svc.task_prepare(
        env.project_id,
        purpose="meeting_extract",
        meeting_id=spec["meeting"],
        request_id=new_request_id(),
    )
    context = task["context"]
    assert "seg-00001 [00:00:00.000 - 00:00:02.000]" in context
    assert "Compare the controllers under actuator limits." in context
    assert "seg-00003 [00:00:06.000 - 00:00:08.000] (uncertain)" in context
    assert '"type":"audio"' in context
    assert '"segments"' not in context  # the payload JSON itself is not dumped
    items = [
        item(spec, "suggestion", "Compare the controllers under actuator limits.", 0, 2000),
        item(spec, "commitment", "Send the constrained result.", 4000, 6000),
    ]
    result = env.svc.task_submit(
        env.project_id, task["task_id"], json.dumps(
            {"purpose": "meeting_extract", "items": items}
        ).encode(), new_request_id(),
    )  # fmt: skip
    assert result["ok"] and len(result["results"]) == 3
    # Exact meeting + locator is the ingestion identity: proposing the same span again fails.
    with pytest.raises(PrometheosError) as dup:
        submit_items(env, spec, [items[0]])
    assert dup.value.code == "revision_conflict"
    # A span that the transcript does not contain is refused at the same gate.
    with pytest.raises(PrometheosError, match=r"beyond|no transcript segment"):
        submit_items(env, spec, [item(spec, "question", "Anything?", 2100, 3900)])


def test_reconcile_shows_the_cited_transcript_text_and_later_corrections(env: AudioEnv) -> None:
    spec = meeting_from_audio(env, "normal")
    submit_items(
        env,
        spec,
        [
            item(spec, "suggestion", "Compare the controllers.", 0, 2000),
            item(spec, "commitment", "Send the constrained result.", 4000, 6000),
        ],
    )
    rows = env.svc.reconcile(env.project_id, OWNER, spec["meeting"])["items"]
    by_text = {r["text"]: r for r in rows}
    first = by_text["Compare the controllers."]["audio_spans"][0]
    assert first["quote"] == "Compare the controllers under actuator limits."
    assert first["clock"] == "00:00:00.000 - 00:00:02.000" and first["segment_ids"] == ["seg-00001"]
    assert first["newer_version_id"] is None and first["text_changed_since"] is False

    corrected = env.svc.transcript_correct(
        env.project_id,
        OWNER,
        spec["transcript"],
        [transcribe.Edit("seg-00002", text="Send the constrained-plant result.")],
        new_request_id(),
    )
    rows = env.svc.reconcile(env.project_id, OWNER, spec["meeting"])["items"]
    by_text = {r["text"]: r for r in rows}
    changed = by_text["Send the constrained result."]["audio_spans"][0]
    untouched = by_text["Compare the controllers."]["audio_spans"][0]
    # The proposal still cites the version it was made from; the view says it has moved on.
    assert changed["version_id"] == spec["transcript"]
    assert changed["quote"] == "Send the constrained result."
    assert changed["newer_version_id"] == corrected["version_id"]
    assert changed["text_changed_since"] is True
    assert untouched["newer_version_id"] == corrected["version_id"]
    assert untouched["text_changed_since"] is False


def test_unsaid_things_stay_unknown_for_owner_and_due(env: AudioEnv) -> None:
    spec = meeting_from_audio(env, "normal")
    submit_items(env, spec, [item(spec, "commitment", "Send the constrained result.", 4000, 6000)])
    row = env.svc.reconcile(env.project_id, OWNER, spec["meeting"])["items"][0]
    assert row["owner"] == "unknown" and row["due"] == "unknown"
    assert row["status"] == "proposed"  # transcription establishes no commitment or identity


# ----- canary: restricted audio never reaches the agent surface, purge reaches everything ------


def store_text(env: AudioEnv) -> list[Path]:
    hits = []
    for path in env.store_dir.rglob("*"):
        if path.is_file() and CANARY.encode() in path.read_bytes():
            hits.append(path)
    return hits


def test_restricted_transcript_canary_stays_off_every_agent_path(env: AudioEnv) -> None:
    script = (f"The secret is {CANARY}.", "(silence)", "Second line.", "(quiet)")
    spec = meeting_from_audio(env, "restricted", script)
    assert store_text(env), "the canary is in the owner's store (sanity)"
    assert env.svc.search(env.project_id, AGENT, CANARY)["hits"] == []  # the query is echoed
    assert env.svc.search(env.project_id, AGENT, "Second")["hits"] == []
    surfaces = [
        env.svc.context(env.project_id, AGENT, purpose="orient", budget=12000),
        env.svc.notebook(env.project_id, AGENT),
        env.svc.list_records(env.project_id, AGENT, kind="version", limit=100),
        env.svc.list_records(env.project_id, AGENT, kind="meeting", limit=100),
        env.svc.next_steps(env.project_id, AGENT),
        env.svc.changes(env.project_id, AGENT, 0),
    ]
    for payload in surfaces:
        assert CANARY not in json.dumps(payload, default=str)
    for hidden in (spec["transcript"], spec["meeting"], spec["audio"]["version_id"]):
        with pytest.raises(PrometheosError) as err:
            env.svc.get(env.project_id, AGENT, hidden, None)
        assert err.value.code == "not_found"
        with pytest.raises(PrometheosError):
            env.svc.transcript_get(env.project_id, AGENT, hidden)
    task = env.svc.task_prepare(
        env.project_id, purpose="notebook_draft", request_id=new_request_id()
    )
    assert CANARY not in json.dumps(task, default=str)


def test_purge_removes_audio_transcript_chunks_cache_and_sessions(env: AudioEnv) -> None:
    script = (f"Remember {CANARY} now.", "(silence)", "Other.", "(quiet)")
    spec = meeting_from_audio(env, "restricted", script)
    # A second recording whose transcription was interrupted leaves chunk rows holding text.
    env.audio_file("half.wav")
    half = env.svc.audio_import(
        env.project_id, OWNER, root_alias="research", path="half.wav", sensitivity="restricted"
    )
    run = env.svc.transcribe_start(
        env.project_id, OWNER, half["version_id"], env.synthetic_config(script=script)
    )

    def crash(index: int) -> None:
        raise transcribe.Interrupted

    with pytest.raises(transcribe.Interrupted):
        transcribe.execute(
            env.svc.open(env.project_id), env.svc.home, run["run_id"], owner="p", after_chunk=crash
        )
    assert env.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 1
    cached = list((env.store_dir / "cache").glob("pcm-*.raw"))
    assert cached, "normalised audio is cached while a run is unfinished"

    plan = env.svc.purge_plan(
        env.project_id, [spec["audio"]["source_id"], half["source_id"]], OWNER
    )
    closure = set(plan["closure"])
    assert {spec["audio"]["version_id"], spec["transcript"], spec["transcript_source"],
            spec["meeting"], half["version_id"]} <= closure  # fmt: skip
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    receipt = env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert receipt["purge_receipt"]["records_purged"] == len(closure)
    assert store_text(env) == []  # database, media, cache and staging: no remnant of the text
    assert env.query("SELECT COUNT(*) FROM transcription_chunks")[0][0] == 0
    assert (
        env.query("SELECT COUNT(*) FROM operation_runs WHERE operation = 'audio_transcribe'")[0][0]
        == 0
    )
    assert env.svc.search(env.project_id, OWNER, CANARY)["hits"] == []
    for rid in (spec["transcript"], spec["transcript_source"], spec["meeting"]):
        tomb = env.svc.get(env.project_id, OWNER, rid, None)
        assert tomb["lifecycle"] == "deleted" and tomb["body"].get("purged") is True
    assert list((env.store_dir / "cache").glob("pcm-*.raw")) == []


def test_purging_a_recording_session_row_goes_with_its_audio(env: AudioEnv) -> None:
    from audio_support import MIC, ScriptedBackend, ScriptedSource

    from prometheos.audio import recorder

    backend = ScriptedBackend({"mic-1": ScriptedSource(MIC, 20)})
    session = env.svc.recording_start(
        env.project_id,
        OWNER,
        recorder.SessionRequest(
            f"Call about {CANARY}", ("mic-1",), "restricted", consent_acknowledged=True
        ),
        backend,
        authority=OWNER_ACT,
    )
    final = env.svc.recording_run(env.project_id, session["session_id"], backend, stall_seconds=30)
    assert final["state"] == "imported"
    assert CANARY in json.dumps(env.query("SELECT title FROM recording_sessions"))
    version = env.svc.get(env.project_id, OWNER, final["version_id"], None)
    plan = env.svc.purge_plan(env.project_id, [version["body"]["source_id"]], OWNER)
    grant = env.svc.grant(
        env.project_id, operation="purge", digest=plan["plan_sha256"], label="SYNTHETIC-TEST"
    )
    env.svc.purge_commit(env.project_id, plan["plan_id"], grant.secret)
    assert env.query("SELECT COUNT(*) FROM recording_sessions")[0][0] == 0
    assert store_text(env) == []
