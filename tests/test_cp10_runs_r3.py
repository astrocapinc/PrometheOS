"""Carried lows closed in CP10.5 (decision 0021, "Carried lows").

CP10.3 review round 2 (review-precheck-cp10-3-a-r2): R2-L2 a resume arriving while the
dispatcher releases a run's slot is not dropped; R2-L3 the surviving mutants S2 (agent views
scrub stored text), S5 (the engine install marks its effect), S6 (the waiting-run take-over
matches parameters), an install interrupted for real before its effect, and the owner-rank CLI
installs checked by behaviour; R2-L4 recovery confirms a locally completed install instead of
reporting an unknown outcome. Integration review round 2 (review-integration-cp10-a-r2):
R2-L1 the POSIX path rule applies to diagnostic run text only; R2-I1 the hub's own path keys.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from audio_support import AudioEnv
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from provider_support import ProviderEnv, completion
from test_cp10_runs_r2 import PAST, _grant, expire, plan_queued, refused, sql, stored

from prometheos.app import runs, texengine
from prometheos.audio import engines, models, transcribe


@pytest.fixture
def audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AudioEnv:
    monkeypatch.setenv(engines.SYNTHETIC_ENV, "1")
    return AudioEnv(tmp_path)


def _env(env: Any) -> runs.Env:
    return runs.Env(env.svc, env.project_id, env.svc.open(env.project_id))


# ----- R2-L2: the dispatcher keeps a resume that arrives during the slot release ----------------


def test_a_resume_in_the_slot_release_window_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    rid = plan_queued(env)
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def executing(e: runs.Env, run_id: str, *, actor: str = "x") -> None:
        calls.append(run_id)
        if len(calls) == 1:
            started.set()
            release.wait(10)
        # The row stays queued: a resume re-queued the run while this execution ended.

    monkeypatch.setattr(runs, "execute", executing)
    renv = _env(env)
    runs.execute_in_background(renv, rid, actor="test")
    assert started.wait(10)
    runs.execute_in_background(renv, rid, actor="test")  # the resume's submission
    release.set()
    deadline = time.monotonic() + 10
    while len(calls) < 2:
        assert time.monotonic() < deadline, "the resumed run was dropped by the dispatcher"
        time.sleep(0.02)
    assert calls == [rid, rid]


def test_a_finished_run_is_not_executed_again_after_its_slot_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    rid = plan_queued(env)
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def executing(e: runs.Env, run_id: str, *, actor: str = "x") -> None:
        calls.append(run_id)
        started.set()
        release.wait(10)
        sql(env.store_dir, "UPDATE operation_runs SET state = 'cancelled', finished_at = ? "
            "WHERE id = ?", (PAST, run_id))  # fmt: skip

    monkeypatch.setattr(runs, "execute", executing)
    renv = _env(env)
    runs.execute_in_background(renv, rid, actor="test")
    assert started.wait(10)
    runs.execute_in_background(renv, rid, actor="test")
    release.set()
    time.sleep(0.5)
    assert calls == [rid]  # rechecked: the run had ended, so nothing ran twice


# ----- R2-L3 S2: agent views scrub stored text --------------------------------------------------


def test_agent_run_views_label_local_paths_even_in_stored_text(tmp_path: Path) -> None:
    env = Env(tmp_path)
    rid = plan_queued(env)
    # The labels belong to the configured roots, not to wherever pytest put ``tmp_path`` (its
    # basetemp need not be the configured temporary folder). A path under the configured
    # temporary folder is built from ``tempfile.gettempdir()``, the very root the scrubber
    # reads (the folder need not exist); a path under no known root is ``<local path>``.
    under_temp = Path(tempfile.gettempdir()) / "p10-known-root" / "private notes" / "draft.txt"
    unrelated = "\\\\unrelated-host\\share\\private notes\\draft.txt"  # UNC: under no known root
    under_pytest = f"{tmp_path}\\private notes\\draft.txt"  # whichever root pytest used
    stored_text = [
        f"could not read {under_temp}",
        f"could not read {unrelated}",
        f"could not read {under_pytest}",
    ]
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        # Text written before the write-time scrub existed (or by any other writer).
        for index, raw in enumerate(stored_text, start=99):
            conn.execute(
                "INSERT INTO operation_run_logs (run_id, idx, at, level, message) VALUES "
                "(?, ?, ?, 'error', ?)",
                (rid, index, PAST, raw),
            )
        conn.execute(
            "UPDATE operation_runs SET limitations = ? WHERE id = ?",
            (json.dumps(stored_text), rid),
        )
        conn.commit()
    for caller in (AGENT, OWNER):
        data = env.svc.run_get(env.project_id, caller, rid)
        view = json.dumps(data)
        for path in (str(tmp_path), str(under_temp.parent), unrelated, "private notes"):
            assert path not in view and path.replace("\\", "\\\\") not in view, path
        shown = [entry["message"] for entry in data["logs"] if entry["idx"] >= 99]
        assert shown[0] == "could not read <temp>"  # the configured temporary folder is labelled
        assert shown[1] == "could not read <local path>"  # an unrelated absolute path is too
        # Whichever root pytest's basetemp is under (the temporary folder, the user's home, the
        # PrometheOS home) or none of them, the path is labelled and never echoed.
        assert re.fullmatch(
            r"could not read <(temp|user home|PrometheOS home|local path)>", shown[2]
        ), shown[2]
        assert data["limitations"] == shown


# ----- R2-L3 S5 and R2-L4: installs stopped during or after their effect -------------------------


def _engine_run(audio: AudioEnv) -> str:
    digest = audio.svc.tex_engine_plan()["plan_digest"]
    created = audio.svc.tex_engine_install(
        digest, authority=OWNER_ACT, project_id=audio.project_id, caller=OWNER, mode="none"
    )
    return str(created["run_id"])


def test_an_engine_install_stopped_mid_effect_is_unknown_external(
    audio: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(audio.home / "missing-engine"))
    attempts: list[str] = []

    def stops(home: Path, digest: str, archive: Path | None = None) -> Any:
        attempts.append(digest)
        raise transcribe.Interrupted  # the worker dies during the download

    monkeypatch.setattr(texengine, "install", stops)
    rid = _engine_run(audio)
    with pytest.raises(transcribe.Interrupted):
        runs.execute(_env(audio), rid)
    expire(audio.store_dir, rid)
    view = audio.svc.run_get(audio.project_id, OWNER, rid)
    assert view["state"] == "unknown_external" and view["external_effect"] == "uncertain"
    assert refused(lambda: audio.svc.run_resume(audio.project_id, OWNER, rid)).code == (
        "validation_error"
    )
    assert len(attempts) == 1


@pytest.mark.parametrize("which", ["engine", "model"])
def test_a_locally_completed_install_is_confirmed_by_recovery(
    audio: AudioEnv, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """R2-L4: the worker stopped after the install completed; recovery verifies it on this
    computer and completes the run (nothing is repeated, nothing for the owner to clear)."""
    if which == "engine":
        monkeypatch.setenv(texengine.ENGINE_DIR_ENV, str(audio.home / "missing-engine"))
        monkeypatch.setattr(
            texengine, "install", lambda *a, **k: (_ for _ in ()).throw(transcribe.Interrupted)
        )
        rid = _engine_run(audio)
    else:
        monkeypatch.setenv(models.MODELS_DIR_ENV, str(audio.home / "models"))
        monkeypatch.setattr(
            models, "install", lambda *a, **k: (_ for _ in ()).throw(transcribe.Interrupted)
        )
        plan = audio.svc.audio_models_plan("faster-whisper-base")
        rid = audio.svc.audio_models_install(
            "faster-whisper-base", plan["plan_sha256"], authority=OWNER_ACT,
            project_id=audio.project_id, caller=OWNER, mode="none",
        )["run_id"]  # fmt: skip
    with pytest.raises(transcribe.Interrupted):
        runs.execute(_env(audio), rid)
    # The files were in place and verified when the worker died.
    if which == "engine":
        real = texengine.status
        monkeypatch.setattr(texengine, "status", lambda home: real(home) | {"state": "installed"})
    else:
        monkeypatch.setattr(models, "installed", lambda home, spec: True)
        monkeypatch.setattr(models, "_verify", lambda target: True)
    expire(audio.store_dir, rid)
    view = audio.svc.run_get(audio.project_id, OWNER, rid)
    assert view["state"] == "completed" and view["external_effect"] == "confirmed"
    assert "verified its result on this computer" in view["limitations"][-1]
    assert view["result"] is not None


def test_an_unverified_install_stays_unknown_external(
    audio: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        models, "install", lambda *a, **k: (_ for _ in ()).throw(transcribe.Interrupted)
    )
    plan = audio.svc.audio_models_plan("faster-whisper-base")
    rid = audio.svc.audio_models_install(
        "faster-whisper-base", plan["plan_sha256"], authority=OWNER_ACT,
        project_id=audio.project_id, caller=OWNER, mode="none",
    )["run_id"]  # fmt: skip
    with pytest.raises(transcribe.Interrupted):
        runs.execute(_env(audio), rid)
    monkeypatch.setattr(models, "installed", lambda home, spec: True)
    monkeypatch.setattr(models, "_verify", lambda target: False)  # a file does not match
    expire(audio.store_dir, rid)
    assert audio.svc.run_get(audio.project_id, OWNER, rid)["state"] == "unknown_external"


def test_an_install_really_interrupted_before_its_effect_resumes_and_installs_once(
    audio: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fake_install(home: Path, key: str, plan_sha256: str, progress: Any = None) -> Any:
        attempts.append(key)
        return {"model": key, "status": "installed", "location": str(home / "models" / key)}

    monkeypatch.setattr(models, "install", fake_install)
    real_started = runs.RunContext.external_started
    dies = {"once": True}

    def dies_before_the_effect(self: runs.RunContext, *args: Any, **kwargs: Any) -> None:
        if dies.pop("once", False):
            raise transcribe.Interrupted  # the worker stops before marking the effect
        real_started(self, *args, **kwargs)

    monkeypatch.setattr(runs.RunContext, "external_started", dies_before_the_effect)
    plan = audio.svc.audio_models_plan("faster-whisper-base")
    rid = audio.svc.audio_models_install(
        "faster-whisper-base", plan["plan_sha256"], authority=OWNER_ACT,
        project_id=audio.project_id, caller=OWNER, mode="none",
    )["run_id"]  # fmt: skip
    with pytest.raises(transcribe.Interrupted):
        runs.execute(_env(audio), rid)
    assert stored(audio.store_dir, rid).state == "running" and attempts == []
    expire(audio.store_dir, rid)
    interrupted = audio.svc.run_get(audio.project_id, OWNER, rid)
    assert interrupted["state"] == "queued" and interrupted["external_effect"] == "none"
    done = audio.svc.run_resume(audio.project_id, OWNER, rid, mode="foreground")
    assert done["state"] == "completed" and attempts == ["faster-whisper-base"]


# ----- R2-L3: owner-rank CLI installs, by behaviour --------------------------------------------


def test_owner_cli_installs_are_recorded_at_owner_rank(
    audio: AudioEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from prometheos import cli, cli_latex

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm_owner", lambda *a, **k: None)
    monkeypatch.setattr(cli_latex, "_confirm_owner", lambda *a, **k: None)
    monkeypatch.setattr(
        models, "install",
        lambda home, key, plan_sha256, progress=None: {
            "model": key, "status": "installed", "location": str(home / "models" / key)
        },
    )  # fmt: skip
    monkeypatch.setattr(
        texengine, "install",
        lambda home, digest, archive=None: {"installed": True, "state": "installed",
                                            "location": str(home / "tools")},
    )  # fmt: skip
    monkeypatch.setenv("PROMETHEOS_HOME", str(audio.home))
    plan = audio.svc.audio_models_plan("faster-whisper-base")
    digest = audio.svc.tex_engine_plan()["plan_digest"]
    runner = CliRunner()
    for args in (
        ["audio", "model", "install", "--plan-sha256", plan["plan_sha256"]],
        ["tex", "engine", "install", "--digest", digest],
    ):
        done = runner.invoke(cli.app, [*args, "--project", audio.project_id, "--json"])
        assert done.exit_code == 0, done.output
    owner_runs = audio.svc.run_list(audio.project_id, OWNER)["runs"]
    installs = [r for r in owner_runs if r["operation"].endswith("_install")]
    assert {r["operation"] for r in installs} == {"audio_models_install", "tex_engine_install"}
    agent_ids = {r["run_id"] for r in audio.svc.run_list(audio.project_id, AGENT)["runs"]}
    for run in installs:
        assert run["run_id"] not in agent_ids  # an agent can neither read nor resume them
        assert refused(
            lambda r=run: audio.svc.run_get(audio.project_id, AGENT, r["run_id"])
        ).code == ("not_found")


# ----- R2-L3 S6: the take-over matches the waiting run's parameters -----------------------------


def test_the_takeover_matches_the_waiting_runs_parameters(tmp_path: Path) -> None:
    env = ProviderEnv(tmp_path)
    env.configure()
    shown = env.preview(AGENT)
    params = {"purpose": "meeting_extract", "meeting_id": env.meeting["meeting_id"],
              "context_budget": 12000, "digest": shown["digest"]}  # fmt: skip
    approval_id = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "provider_run", params)
    ).details["approval_request_id"]
    (real,) = env.svc.run_list(env.project_id, AGENT, operation="provider_run")["runs"]
    stray, _ = runs.create(
        _env(env), AGENT, "provider_run",
        {"purpose": "meeting_extract", "meeting_id": env.meeting["meeting_id"],
         "context_budget": 9000, "include_restricted": False,
         "approved_digest": shown["digest"], "acknowledge_unknown": None},
        waiting_on={"kind": "approval", "approval_request_id": approval_id},
        fingerprint=runs.Fingerprint(inputs=(("payload", "0" * 64),), config={}),
    )  # fmt: skip
    # The stray waits longer, so it is the first candidate a take-over could pick.
    sql(env.store_dir, "UPDATE operation_runs SET created_at = ? WHERE id = ?", (PAST, stray.id))
    _grant(env, approval_id)
    env.script(completion(env.items_reply(2, 3)))
    env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert env.svc.run_get(env.project_id, AGENT, real["run_id"])["state"] == "completed"
    assert env.svc.run_get(env.project_id, AGENT, stray.id)["state"] == "cancelled"
    assert len(env.transport.requests) == 1


# ----- integration R2-L1: POSIX-like researcher text in results stays readable ------------------


def test_run_results_keep_posix_like_text_while_logs_still_label_paths(tmp_path: Path) -> None:
    env = Env(tmp_path)
    rid = plan_queued(env)
    done = env.svc.run_resume(env.project_id, AGENT, rid, mode="foreground")
    assert done["state"] == "completed"
    note = "use the /v1/ prefix, run with -o /dev/null and read /docs/guide"
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        row = conn.execute(
            "SELECT output FROM operation_run_stages WHERE run_id = ? AND name = 'result'", (rid,)
        ).fetchone()
        result = json.loads(row[0]) | {"note": note}
        conn.execute(
            "UPDATE operation_run_stages SET output = ? WHERE run_id = ? AND name = 'result'",
            (json.dumps(result), rid),
        )
        conn.execute(
            "INSERT INTO operation_run_logs (run_id, idx, at, level, message) VALUES "
            "(?, 98, ?, 'warning', ?)",
            (rid, PAST, "worker stopped reading /home/someone/research/x.csv"),
        )
        conn.commit()
    for caller in (AGENT, OWNER):
        view = env.svc.run_get(env.project_id, caller, rid)
        assert view["result"]["note"] == note  # researcher data, not a diagnostic
        logs = " ".join(line["message"] for line in view["logs"])
        assert "/home/someone" not in logs  # diagnostic text keeps the POSIX rule


# ----- integration R2-I1: the hub's own path keys ---------------------------------------------


def test_the_hub_drops_absolute_archive_and_paths_values() -> None:
    from prometheos.hub.redaction import Redactor, is_path_key

    assert is_path_key("archive") and is_path_key("paths")
    shown = Redactor(lambda: [], lambda: []).body(
        {"archive": "C:\\Users\\r\\tectonic.zip", "paths": ["C:\\Users\\r\\a.tex", "rel/b.tex"],
         "note": "C:\\Users\\r\\kept as content"},
        envelope=False,
    )  # fmt: skip
    assert shown == {"paths": ["rel/b.tex"], "note": "C:\\Users\\r\\kept as content"}
