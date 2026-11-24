"""One free-text path rule for the hub and for run text (decision 0019; CP10.3 review R2-L1).

``runs.scrub`` guards run logs and errors on every transport, MCP included, where the hub's
middleware never runs. It now uses the hub's rule (:mod:`prometheos.domain.redact`): a path
is consumed whole, spaces included, to its closing quote or the end of the line, and a known
root keeps its label. Route text and network URLs are left alone.

Integration review round 2 (decision 0019 "Round 2"): INT-CP10-L1, run text also rewrites
MSYS, WSL and other absolute POSIX-style paths; INT-CP10-M1, path-valued fields never show an
absolute value, and every run view (owner included) labels the paths in its parameters.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from cp1_support import OWNER, OWNER_ACT, Env

from prometheos.app import runs
from prometheos.domain import redact
from prometheos.domain.models import PrometheosError
from prometheos.hub import redaction
from prometheos.hub.redaction import Redactor, normalise_roots

SPACED = "C:\\Users\\John Smith\\Secret Project\\notes.txt"
UNC = "\\\\fileserver\\Lab Share\\Secret Project\\data.csv"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "Prometheos Home"
    folder.mkdir()
    monkeypatch.setenv("PROMETHEOS_HOME", str(folder))
    return folder


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f"cannot open {SPACED}", "cannot open <local path>"),
        (f"cannot open '{SPACED}' (locked)", "cannot open '<local path>' (locked)"),
        (f'cannot open "{SPACED}": denied', 'cannot open "<local path>": denied'),
        (f"copy to {UNC} failed", "copy to <local path>"),
        (f"first {SPACED}\nsecond line kept", "first <local path>\nsecond line kept"),
        ("C:/Users/John Smith/x.txt", "<local path>"),
    ],
)
def test_a_path_is_consumed_whole(text: str, shown: str) -> None:
    assert runs.scrub(text) == shown
    for part in ("John", "Smith", "Secret", "fileserver", "Lab Share"):
        assert part not in runs.scrub(text)


@pytest.mark.parametrize(
    "text",
    [
        "GET /api/v1/runs/oprun_x answered 404",
        "see https://example.org/a/b and http://localhost:8080/x",
        "ratio 3/4 of 12 stages",
        "",
    ],
)
def test_route_text_and_urls_are_kept(text: str) -> None:
    assert runs.scrub(text) == text


def test_known_roots_keep_their_label(home: Path) -> None:
    inside = home / "projects" / "p 1" / "record.sqlite"
    assert runs.scrub(f"locked: '{inside}'") == "locked: '<PrometheOS home>'"
    temp = Path(tempfile.gettempdir()) / "scratch dir" / "x"
    assert runs.scrub(f"at {temp}") == "at <temp>"
    profile = Path.home() / "Documents" / "My Thesis" / "draft.tex"
    assert runs.scrub(f"at {profile}") == "at <user home>"


def test_scrub_walks_nested_values() -> None:
    value = {"message": f"at {SPACED}", "items": [SPACED, 3, None], "n": 1}
    assert runs.scrub(value) == {
        "message": "at <local path>",
        "items": ["<local path>", 3, None],
        "n": 1,
    }


def test_the_hub_and_run_text_share_one_rule(home: Path) -> None:
    """With the same roots, the hub's error-body rewrite and run text agree, and the hub
    module uses the shared implementation rather than a copy."""
    assert redaction.FreeText is redact.FreeText
    assert redaction.normalise_roots is redact.normalise_roots
    hub = Redactor(
        lambda: normalise_roots([(home, "<PrometheOS home>")]),
        lambda: normalise_roots([(Path.home(), "<user home>"), (tempfile.gettempdir(), "<temp>")]),
    )
    samples = [
        f"cannot open '{SPACED}' (locked)",
        f"copy to {UNC} failed",
        f"store at {home / 'projects' / 'a b'} is busy",
        f"profile {Path.home() / 'x y' / 'z'}",
        "GET /api/v1/runs/oprun_x",
    ]
    for text in samples:
        envelope = {"ok": False, "error": {"code": "io_error", "message": text}}
        shown = hub.body(envelope, envelope=True)["error"]["message"]
        assert shown == runs.scrub(text), text


# ----- round 2: POSIX-style spellings (INT-CP10-L1) ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("at /home/u/z/w", "at <local path>"),
        ("open /c/Users/John Smith/Secret Project/x.txt", "open <local path>"),
        ("open '/mnt/c/Users/John Smith/x.txt' (busy)", "open '<local path>' (busy)"),
        ('read "/var/lib/lab data/run 1.csv": denied', 'read "<local path>": denied'),
        ("first /srv/a b/c\nsecond kept", "first <local path>\nsecond kept"),
    ],
)
def test_run_text_rewrites_posix_style_paths(text: str, shown: str) -> None:
    assert runs.scrub(text) == shown
    for part in ("John", "Smith", "lab data", "/home/u", "/srv"):
        assert part not in runs.scrub(text)


@pytest.mark.parametrize(
    "text",
    [
        "POST /api/v1/runs/oprun_x/resume answered 409",
        "see https://example.org/a/b/c and file at ./rel/x/y or ~/notes/a",
        "paper/main.tex line 3",
        "and/or 3/4/5",
    ],
)
def test_posix_rule_keeps_routes_urls_and_relative_paths(text: str) -> None:
    assert runs.scrub(text) == text


def test_the_hub_rule_is_unchanged_for_posix_text() -> None:
    """The hub's error-body rule does not take the POSIX alternative (its free text carries
    route text; its roots include the research roots)."""
    hub = Redactor(lambda: [], lambda: [])
    text = "see /api/v1/runs/x and /srv/a/b"
    envelope = {"ok": False, "error": {"message": text}}
    assert hub.body(envelope, envelope=True)["error"]["message"] == text


# ----- round 2: path-valued fields and owner run views (INT-CP10-M1) -----------------------------


def test_path_valued_fields_never_show_an_absolute_value(home: Path) -> None:
    assert redact.is_path_key("archive") and redact.is_path_key("paths")
    assert runs.scrub({"archive": "/x"}) == {"archive": "<local path>"}
    assert runs.scrub({"archive": SPACED}) == {"archive": "<local path>"}
    assert runs.scrub({"engine_dir": str(home / "tools" / "t")}) == {
        "engine_dir": "<PrometheOS home>"
    }
    assert runs.scrub({"paths": ["paper/main.tex", "/opt"]}) == {
        "paths": ["paper/main.tex", "<local path>"]
    }
    assert runs.scrub({"path": "paper/main.tex"}) == {"path": "paper/main.tex"}
    hub = Redactor(lambda: [], lambda: [])
    assert hub.body({"archive": SPACED, "plan_digest": "x"}, envelope=False) == {"plan_digest": "x"}


def test_an_owner_install_from_an_archive_shows_no_path_in_any_run_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INT-CP10-M1 reproduction: the stored parameters keep the archive's path (resume
    needs it); the owner's run view and run list never show it (the hub walker checks the
    hub routes, tests/test_hub_no_local_paths.py)."""
    from prometheos.app import texengine

    monkeypatch.delenv(texengine.ENGINE_DIR_ENV, raising=False)
    env = Env(tmp_path)
    archive = tmp_path / "Owner Downloads" / "MarkerArchiveQ9" / "tectonic.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not the pinned release")
    plan = env.svc.tex_engine_plan()
    with pytest.raises(PrometheosError) as err:
        env.svc.tex_engine_install(
            plan["plan_digest"], archive=archive, authority=OWNER_ACT,
            project_id=env.project_id, caller=OWNER, mode="wait",
        )  # fmt: skip
    run_id = err.value.details["run_id"]
    with closing(sqlite3.connect(env.store_dir / "record.sqlite")) as conn:
        stored = json.loads(
            conn.execute("SELECT params FROM operation_runs WHERE id = ?", (run_id,)).fetchone()[0]
        )
    assert stored["archive"] == str(archive)  # kept server-side for resume
    seen = [
        json.dumps(env.svc.run_get(env.project_id, OWNER, run_id)),
        json.dumps(env.svc.run_list(env.project_id, OWNER)),
    ]
    for text in seen:
        assert "MarkerArchiveQ9" not in text and "Owner Downloads" not in text, text[:400]
    owner = env.svc.run_get(env.project_id, OWNER, run_id)
    # A label, never the value (tmp_path lies under the temporary folder: "<temp>").
    assert owner["params"]["archive"] in ("<temp>", "<user home>", "<local path>")
    assert owner["params"]["plan_digest"] == plan["plan_digest"]
