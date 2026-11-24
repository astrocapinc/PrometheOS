"""Follow-ups of review cp9-a round 3 (accepted; decision 0012 "Review round 3 follow-ups").

O1: the plan digest binds the destination remote, so a changed remote invalidates it.
O2: focused regression for the ``git status -z`` rename/copy parsing repair.
O4: a missing ``~<digit>`` name gets its own message, distinct from the 8.3 alias refusal.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cp1_support import OWNER, Env
from cp9_support import LatexEnv
from test_cp9_sync import Linked, git

from prometheos.app import gitsync
from prometheos.domain.models import PrometheosError


@pytest.fixture
def linked(tmp_path: Path) -> Linked:
    return Linked(tmp_path)


def test_a_changed_remote_invalidates_the_push_plan(linked: Linked, tmp_path: Path) -> None:
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Hyphenate")
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert preview["plan"]["remote_url"] == str(linked.remote.bare)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    # The remote is swapped for another repository with the same history.
    other = tmp_path / "elsewhere.git"
    shutil.copytree(linked.remote.bare, other)
    git(linked.folder, "remote", "set-url", "origin", str(other))
    before = git(other, "rev-parse", "refs/heads/master")
    with pytest.raises(PrometheosError) as changed:
        linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    assert changed.value.code == "revision_conflict"
    assert git(other, "rev-parse", "refs/heads/master") == before
    assert linked.remote.head() == before
    # A pull preview binds the remote as well.
    pull = linked.svc.overleaf_pull_preview(linked.pid, OWNER, linked.paper_id)
    assert pull["plan"]["remote_url"] == str(other)


def test_status_parsing_keeps_both_paths_of_a_rename_and_a_copy(linked: Linked) -> None:
    runner = gitsync.Git(linked.folder, linked.env.home)
    git(linked.folder, "mv", "refs.bib", "references-renamed.bib")
    (linked.folder / "copy-of-intro.tex").write_text(
        (linked.folder / "sections" / "intro.tex").read_text("utf-8"), "utf-8"
    )
    git(linked.folder, "add", "copy-of-intro.tex")
    changes = gitsync._local_changes(runner, limit=None)
    assert "references-renamed.bib" in changes and "refs.bib" in changes
    assert "copy-of-intro.tex" in changes
    # The old parser cut three characters off the original path of a rename.
    assert "s.bib" not in changes


def test_missing_tilde_digit_names_have_their_own_message(tmp_path: Path) -> None:
    ws = LatexEnv(Env(tmp_path))
    with pytest.raises(PrometheosError) as lookalike:
        ws.write("draft~1.tex", "x", None, OWNER)
    assert "followed by a digit" in lookalike.value.message
    (ws.folder / ".git").mkdir()
    from test_cp9_review_cp9a_r2 import short_alias

    alias = short_alias(ws.folder / ".git", "")
    if alias:  # a real 8.3 alias of an existing folder: the alias message
        with pytest.raises(PrometheosError) as short:
            ws.write(f"{alias}/x.tex", "x", None, OWNER)
        assert "short (8.3) file names are refused" in short.value.message
