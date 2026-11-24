"""Integration review fixes (decision 0012 "Integration review fixes").

CP9 F1: an approved push goes to the explicit URL bound in the plan. Push URLs and URL
rewrites are refused from every configuration scope; environment-borne configuration
never reaches git. CP5 INT1-M3: no hub GET route ever returns an absolute local path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cp1_support import OWNER
from test_cp9_sync import Linked, git

from prometheos.domain.models import PrometheosError


@pytest.fixture
def linked(tmp_path: Path) -> Linked:
    return Linked(tmp_path)


def _committed(linked: Linked) -> None:
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Hyphenate")


def _decoy(linked: Linked, tmp_path: Path) -> Path:
    decoy = tmp_path / "decoy.git"
    shutil.copytree(linked.remote.bare, decoy)
    return decoy


def _push_refused(linked: Linked) -> None:
    with pytest.raises(PrometheosError) as refused:
        linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert refused.value.code == "policy_denied"


@pytest.mark.parametrize(
    ("key", "value_of"),
    [
        ("remote.origin.pushurl", lambda bare, decoy: str(decoy)),
        (None, "insteadof"),
        (None, "pushinsteadof"),
    ],
)
def test_local_push_redirections_are_refused(
    linked: Linked, tmp_path: Path, key: str | None, value_of: object
) -> None:
    _committed(linked)
    decoy = _decoy(linked, tmp_path)
    before = (linked.remote.head(), git(decoy, "rev-parse", "refs/heads/master"))
    if key is not None:
        git(linked.folder, "config", "--local", key, value_of(linked.remote.bare, decoy))  # type: ignore[operator]
    else:
        git(linked.folder, "config", "--local", f"url.{decoy}.{value_of}", str(linked.remote.bare))
    status = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert status["blocked_settings"], status
    _push_refused(linked)
    assert (linked.remote.head(), git(decoy, "rev-parse", "refs/heads/master")) == before


def test_global_config_cannot_redirect_a_push(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _committed(linked)
    decoy = _decoy(linked, tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".gitconfig").write_text(
        f'[url "{decoy.as_posix()}"]\n\tpushInsteadOf = {linked.remote.bare.as_posix()}\n',
        "utf-8",
    )
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    status = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert any(s.startswith("global:url.") for s in status["blocked_settings"]), status
    _push_refused(linked)


def test_environment_config_never_reaches_git(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _committed(linked)
    decoy = _decoy(linked, tmp_path)
    evil = tmp_path / "evil.gitconfig"
    evil.write_text(f'[remote "origin"]\n\tpushurl = {decoy.as_posix()}\n', "utf-8")
    for name, value in (
        ("GIT_CONFIG_GLOBAL", str(evil)),
        ("GIT_CONFIG_SYSTEM", str(evil)),
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "remote.origin.pushurl"),
        ("GIT_CONFIG_VALUE_0", str(decoy)),
        ("GIT_CONFIG_PARAMETERS", f"'remote.origin.pushurl'='{decoy}'"),
    ):
        monkeypatch.setenv(name, value)
    decoy_before = git(decoy, "rev-parse", "refs/heads/master")
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert preview["plan"]["remote_url"] == str(linked.remote.bare)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    linked.svc.overleaf_push(linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret)
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    assert linked.remote.head() == git(linked.folder, "rev-parse", "HEAD")
    assert git(decoy, "rev-parse", "refs/heads/master") == decoy_before


def test_push_goes_to_the_url_in_the_plan_not_the_remote_name(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even if a push URL slipped past the refusal, the argv names the
    approved URL, so git never consults remote.origin.pushurl."""
    from prometheos.app import gitsync

    _committed(linked)
    decoy = _decoy(linked, tmp_path)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    git(linked.folder, "config", "--local", "remote.origin.pushurl", str(decoy))
    monkeypatch.setattr(gitsync.Git, "redirects", lambda self, entries=None: [])
    monkeypatch.setattr(gitsync, "RISKY_LOCAL", [])
    decoy_before = git(decoy, "rev-parse", "refs/heads/master")
    linked.svc.overleaf_push(linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret)
    assert linked.remote.head() == git(linked.folder, "rev-parse", "HEAD")
    assert git(decoy, "rev-parse", "refs/heads/master") == decoy_before
