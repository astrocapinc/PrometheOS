"""Integration review run 2 fixes (decision 0012 "Integration review run 2 fixes").

INT2-F1: a remote *named* by the approved URL redirected an approved push. INT2-F2: with
several origin URLs PrometheOS fetched from the first and bound the last. INT2-F3: the CLI
confirmation names the destination. INT2-F4: worktree-scope configuration skipped the
refusal. INT2-I1: coverage of every scope, and configuration checks fail closed. Found while
fixing these: a configured fetch refspec, bundle URI, lazy fetch from a promisor remote or
``push.followTags`` could still change what a fetch or an approved push does.

Every repository here is local. Tests whose configuration holds an https:// URL run under
``offline``, which stops any git subcommand that could contact a remote before it is
spawned, so the refusal is proved to come first and nothing is ever contacted.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import create, paper_body
from test_cp9_sync import Linked, Remote, git
from typer.testing import CliRunner

from prometheos import cli
from prometheos.app import gitsync
from prometheos.domain import text
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError

TOKEN = "olp_SYNTHETICint2CANARY0123456789abcdef"  # a synthetic canary, not a real token
OVERLEAF = "https://git.overleaf.com/0123456789abcdef"
MASKED = "<url-or-path>"
NETWORK = frozenset(
    {"fetch", "push", "clone", "ls-remote", "pull", "remote", "submodule", "fetch-pack"}
)


def _subcommand(args: tuple[str, ...]) -> str:
    items = list(args)
    while items[:1] == ["-c"]:
        items = items[2:]
    return items[0] if items else ""


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record every git invocation PrometheOS makes; a subcommand that could contact a
    remote raises before any process is spawned."""
    calls: list[tuple[str, ...]] = []
    real = gitsync.Git.run

    def recorder(self: gitsync.Git, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        if _subcommand(args) in NETWORK:
            raise AssertionError(f"a network git command was attempted: {_subcommand(args)}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(gitsync.Git, "run", recorder)
    return calls


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary user profile: its .gitconfig is the only global configuration git reads
    here. Test-side git calls lose every inherited GIT_* variable, so none of them can
    write a real configuration file."""
    home = tmp_path / "profile"
    home.mkdir()
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def linked(profile: Path, tmp_path: Path) -> Linked:
    return Linked(tmp_path)


def _global(profile: Path, *args: str) -> None:
    """Write the temporary profile's global configuration (never another file)."""
    git(profile, "config", "--file", str(profile / ".gitconfig"), *args)


def _committed(linked: Linked) -> str:
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Hyphenate")
    return git(linked.folder, "rev-parse", "HEAD")


def _copy(linked: Linked, tmp_path: Path, name: str = "decoy.git") -> Path:
    copy = tmp_path / name
    shutil.copytree(linked.remote.bare, copy)
    return copy


def _heads(*repositories: Path) -> list[str]:
    return [git(r, "rev-parse", "refs/heads/master") for r in repositories]


def _origin(linked: Linked) -> str:
    return git(linked.folder, "config", "--get", "remote.origin.url")


def _status(linked: Linked) -> dict[str, Any]:
    return linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)


def _actions(linked: Linked) -> list[Callable[[], Any]]:
    return [
        lambda: linked.svc.overleaf_pull_preview(linked.pid, OWNER, linked.paper_id),
        lambda: linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id),
        lambda: linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "x"),
    ]


def _refused(action: Callable[[], Any]) -> PrometheosError:
    with pytest.raises(PrometheosError) as refused:
        action()
    assert refused.value.code == "policy_denied", refused.value
    return refused.value


def _present(repository: Path, oid: str) -> bool:
    """Whether an object is in the repository, without letting git fetch it lazily."""
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    done = subprocess.run(
        ["git", "cat-file", "-e", oid], cwd=repository, env=env, capture_output=True, check=False
    )
    return done.returncode == 0


def _no_network(calls: list[tuple[str, ...]]) -> None:
    assert not [c for c in calls if _subcommand(c) in NETWORK]


# ----- INT2-F1: a remote named by the approved URL ----------------------------------------------


@pytest.mark.parametrize("scope", ["local", "global"])
def test_a_remote_named_by_the_approved_url_is_refused(
    linked: Linked, tmp_path: Path, profile: Path, scope: str
) -> None:
    """71c244b: ``[remote "<approved URL>"] url = <decoy>`` passed every check, and git
    sent the approved push (named by URL) to the decoy."""
    pushed = _committed(linked)
    decoy = _copy(linked, tmp_path)
    before = _heads(linked.remote.bare, decoy)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    key = f"remote.{_origin(linked)}.url"
    if scope == "local":
        git(linked.folder, "config", "--local", key, str(decoy))
    else:
        _global(profile, key, str(decoy))
    assert f"{scope}:remote.{MASKED}.url" in _status(linked)["blocked_settings"]
    _refused(
        lambda: linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    )
    for action in _actions(linked):
        _refused(action)
    assert _heads(linked.remote.bare, decoy) == before
    assert pushed not in before


def test_an_overleaf_url_named_remote_is_refused_before_any_network_command(
    linked: Linked, tmp_path: Path, offline: list[tuple[str, ...]]
) -> None:
    decoy = _copy(linked, tmp_path)
    git(linked.folder, "remote", "set-url", "origin", OVERLEAF)
    git(linked.folder, "config", "--local", f"remote.{OVERLEAF}.url", str(decoy))
    status = _status(linked)
    assert status["remote"] == {"name": "origin", "url": OVERLEAF, "url_accepted": True}
    assert f"local:remote.{MASKED}.url" in status["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    _no_network(offline)


@pytest.mark.parametrize(
    ("name", "url_like"),
    [
        (OVERLEAF, True),
        ("file:///C:/papers/overleaf.git", True),
        ("C:\\papers\\overleaf.git", True),
        ("C:/papers/overleaf.git", True),
        ("\\\\server\\share\\overleaf.git", True),  # UNC: neither ':' nor a leading '/'
        ("//server/share/overleaf.git", True),
        ("/srv/git/overleaf.git", True),
        ("origin", False),
        ("team/upstream", False),  # git remote add accepts it; it is never a destination
        ("backup-2026", False),
    ],
)
def test_remote_names_that_could_be_read_as_a_destination(name: str, url_like: bool) -> None:
    """Every URL or path check_remote_url accepts is url-like; names git remote add creates
    are not."""
    assert gitsync._url_like(name) is url_like


# ----- INT2-F2: exactly one origin URL, fetched and pushed by URL ---------------------------------


@pytest.mark.parametrize("case", ["set-url-add", "decoy-first", "global"])
def test_an_origin_with_more_than_one_url_is_refused(
    linked: Linked,
    tmp_path: Path,
    profile: Path,
    offline: list[tuple[str, ...]],
    case: str,
) -> None:
    """71c244b validated, showed and bound the last URL while ``git fetch origin`` used the
    first: a decoy-first list offered the decoy's commits as the approved URL's, and the
    documented ``git remote set-url --add origin <backup>`` sent the push to the backup
    while the hub said Overleaf."""
    pushed = _committed(linked)
    other = _copy(linked, tmp_path, "other.git")
    work = tmp_path / "other-work"
    git(tmp_path, "clone", "-q", str(other), str(work))
    (work / "other-only.tex").write_text("other\n", "utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "Commit only in the other repository")
    git(work, "push", "-q", "origin", "master")
    before = _heads(linked.remote.bare, other)
    origin = _origin(linked)
    if case == "set-url-add":
        git(linked.folder, "remote", "set-url", "--add", "origin", str(other))
    elif case == "decoy-first":
        git(linked.folder, "config", "--local", "--replace-all", "remote.origin.url", str(other))
        git(linked.folder, "config", "--local", "--add", "remote.origin.url", origin)
    else:
        _global(profile, "remote.origin.url", str(other))
    status = _status(linked)
    assert status["remote"] == {"name": "origin", "url": None, "url_accepted": False}
    scopes = "global, local" if case == "global" else "local"  # pre-check round 2 INT2G-F4
    entry = f"remote.origin.url (2 values in {scopes}; keep exactly one)"
    assert entry in status["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    _no_network(offline)
    assert _heads(linked.remote.bare, other) == before
    assert pushed not in before


def test_a_token_bearing_first_origin_url_is_refused_without_a_fetch(
    linked: Linked, offline: list[tuple[str, ...]]
) -> None:
    origin = _origin(linked)
    tokened = f"https://git:{TOKEN}@git.overleaf.com/0123456789abcdef"
    git(linked.folder, "config", "--local", "--replace-all", "remote.origin.url", tokened)
    git(linked.folder, "config", "--local", "--add", "remote.origin.url", origin)
    status = _status(linked)
    assert status["remote"]["url"] is None and status["remote"]["url_accepted"] is False
    errors = [_refused(action) for action in _actions(linked)]
    _no_network(offline)
    shown = str(status) + "".join(str(e.to_response().model_dump()) for e in errors)
    assert TOKEN.casefold() not in shown.casefold()
    for path in linked.env.store_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


def test_the_preview_and_push_name_exactly_the_bound_url(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview attribution: the fetch that computes the plan, the push and the fetch after
    it all name the URL bound in the plan, never the remote name (local remote: they run)."""
    real = gitsync.Git.run
    calls: list[tuple[str, ...]] = []

    def recorder(self: gitsync.Git, *args: str, **kwargs: Any) -> Any:
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(gitsync.Git, "run", recorder)
    linked.remote.edit("refs.bib", "Fixture Press", "Fixture Press Ltd", "Collaborator edit")
    pull = linked.svc.overleaf_pull_preview(linked.pid, OWNER, linked.paper_id)
    linked.svc.overleaf_pull_apply(linked.pid, OWNER, linked.paper_id, pull["digest"])
    pushed = _committed(linked)
    result = linked.grant_and_push()
    network = [c for c in calls if _subcommand(c) in NETWORK]
    assert {_subcommand(c) for c in network} == {"fetch", "push"}
    for args in network:
        assert pull["plan"]["remote_url"] in args and "origin" not in args, args
    assert result["remote_head"] == pushed == linked.remote.head()


def test_fetch_ignores_a_configured_fetch_refspec(linked: Linked) -> None:
    """Fetching by URL with PrometheOS's own refspec: a configured
    ``remote.origin.fetch`` can no longer map another branch onto origin/master and pass
    its commits off as Overleaf's (or slip them into a push unlisted)."""
    collab = linked.remote.collab
    git(collab, "checkout", "-q", "-b", "side")
    (collab / "side-only.tex").write_text("side\n", "utf-8")
    git(collab, "add", "-A")
    git(collab, "commit", "-qm", "Commit on another branch")
    git(collab, "push", "-q", "origin", "side")
    git(
        linked.folder,
        "config",
        "--local",
        "--replace-all",
        "remote.origin.fetch",
        "+refs/heads/side:refs/remotes/origin/master",
    )
    preview = linked.preview()
    assert preview["plan"]["kind"] == "up_to_date", preview["plan"]
    assert git(linked.folder, "rev-parse", "refs/remotes/origin/master") == linked.remote.head()


# ----- INT2-F4: worktree scope, and transport programs from every scope ---------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("remote.origin.uploadpack", "prometheos-int2-canary-upload-pack"),
        ("core.sshCommand", "prometheos-int2-canary-ssh"),
        ("credential.helper", f"!echo password={TOKEN}"),
        ("include.path", "../../elsewhere.gitconfig"),
    ],
)
def test_worktree_scope_configuration_is_refused_like_local(
    linked: Linked, key: str, value: str
) -> None:
    """71c244b listed only ``git config --local``: with extensions.worktreeConfig a
    worktree-scope ``remote.origin.uploadpack`` ran its program during the pull preview."""
    git(linked.folder, "config", "--local", "extensions.worktreeConfig", "true")
    git(linked.folder, "config", "--worktree", key, value)
    assert f"worktree:{key.lower()}" in _status(linked)["blocked_settings"]
    errors = [_refused(action) for action in _actions(linked)]
    shown = "".join(str(e.to_response().model_dump()) for e in errors)
    assert "canary" not in shown and TOKEN not in shown


@pytest.mark.parametrize(
    "key", ["remote.origin.uploadpack", "remote.upstream.receivepack", "remote.origin.vcs"]
)
def test_transport_programs_are_refused_from_every_scope(
    linked: Linked, profile: Path, key: str
) -> None:
    _global(profile, key, "prometheos-int2-canary-program")
    assert f"global:{key}" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


@pytest.mark.parametrize(
    ("key", "value", "shown"),
    [
        ("http.sslVerify", "false", "http.sslverify"),
        (f"http.{OVERLEAF}.sslCAInfo", "C:/planted-ca.pem", f"http.{MASKED}.sslcainfo"),
        ("http.sslCAPath", "C:/planted-cas", "http.sslcapath"),
        ("http.schannelUseSSLCAInfo", "true", "http.schannelusesslcainfo"),
        ("http.curloptResolve", "git.overleaf.com:443:127.0.0.1", "http.curloptresolve"),
    ],
)
def test_the_clone_cannot_weaken_tls_or_resolve_the_host_elsewhere(
    linked: Linked, profile: Path, key: str, value: str, shown: str
) -> None:
    """Resolving git.overleaf.com to another address with verification off, or with a CA
    planted in the clone's own configuration, would hand an approved push and the token Git
    Credential Manager sends to another server. Git applies these settings to the Overleaf
    URL (shown with a configuration query; nothing is contacted), so the clone may not set
    them. The owner's own global settings, such as a corporate CA, stay allowed."""
    git(linked.folder, "config", "--local", key, value)
    applied = git(linked.folder, "config", "--get-urlmatch", "http", OVERLEAF)
    assert value in applied
    assert shown in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    git(linked.folder, "config", "--local", "--unset", key)
    _global(profile, key, value)
    assert _status(linked)["blocked_settings"] == []


def _unlinked(tmp_path: Path) -> tuple[Env, Remote, str]:
    env = Env(tmp_path)
    remote = Remote(tmp_path)
    paper_id = new_id("paper")
    env.apply([create("paper", paper_body(title="Linked paper"), paper_id)])
    return env, remote, paper_id


@pytest.mark.parametrize("case", ["insteadof", "second-url", "uploadpack"])
def test_link_checks_every_scope_before_it_clones(tmp_path: Path, profile: Path, case: str) -> None:
    """The clone itself is a fetch: a rewrite, a second origin URL or an upload program
    from the user's global configuration must stop the link before anything is fetched
    (71c244b cloned first and checked afterwards)."""
    env, remote, paper_id = _unlinked(tmp_path)
    decoy = tmp_path / "decoy.git"
    shutil.copytree(remote.bare, decoy)
    if case == "insteadof":
        _global(profile, f"url.{decoy}.insteadOf", str(remote.bare))
    elif case == "second-url":
        _global(profile, "remote.origin.url", str(decoy))
    else:
        _global(profile, "remote.origin.uploadpack", "prometheos-int2-canary-upload-pack")
    with pytest.raises(PrometheosError) as refused:
        env.svc.overleaf_link(
            env.project_id, OWNER, paper_id, str(remote.bare), "research", "overleaf"
        )
    assert refused.value.code == "policy_denied", refused.value
    assert "canary" not in str(refused.value.to_response().model_dump())
    assert not (env.research / "overleaf").exists()


# ----- fetches go nowhere else: lazy fetch and bundle URIs ---------------------------------------


@pytest.mark.parametrize(
    ("key", "value"), [("remote.evil.promisor", "true"), ("extensions.partialClone", "evil")]
)
def test_promisor_remotes_are_refused(linked: Linked, tmp_path: Path, key: str, value: str) -> None:
    """A promisor remote is another place git fetches from (missing objects, on demand):
    partial clones are refused with a clear message, whatever the git version."""
    git(linked.folder, "config", "--local", "remote.evil.url", str(_copy(linked, tmp_path)))
    git(linked.folder, "config", "--local", key, value)
    assert f"local:{key.lower()}" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


def _nolazy(repository: Path, *args: str) -> str:
    """Test-side git that never fetches a missing object lazily."""
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    return subprocess.run(
        ["git", *args], cwd=repository, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_local_commands_never_fetch_missing_objects_lazily(linked: Linked, tmp_path: Path) -> None:
    """A status view runs local commands even when settings are refused. With a promisor
    remote configured, one that needs a missing object would fetch it from there (verified
    with plain git); the runner's GIT_NO_LAZY_FETCH makes it fail instead."""
    decoy = _copy(linked, tmp_path)
    git(decoy, "config", "uploadpack.allowFilter", "true")
    git(decoy, "config", "uploadpack.allowAnySHA1InWant", "true")
    work = tmp_path / "decoy-work"
    git(tmp_path, "clone", "-q", str(decoy), str(work))
    (work / "decoy-only.tex").write_text("decoy\n", "utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "Commit only in the decoy")
    git(work, "push", "-q", "origin", "master")
    only = git(work, "rev-parse", "HEAD")
    git(linked.folder, "config", "--local", "remote.evil.url", str(decoy))
    git(linked.folder, "config", "--local", "remote.evil.promisor", "true")
    # The decoy's commit without its trees, as the branch head: a status needs its tree.
    _nolazy(linked.folder, "fetch", "-q", "--filter=tree:0", "evil", "+master:refs/evil/master")
    tree = _nolazy(linked.folder, "cat-file", "-p", only).splitlines()[0].split()[1]
    _nolazy(linked.folder, "update-ref", "refs/heads/master", only)
    assert not _present(linked.folder, tree)
    with pytest.raises(PrometheosError):
        _status(linked)
    assert not _present(linked.folder, tree)


def test_a_configured_bundle_uri_is_never_fetched(linked: Linked, tmp_path: Path) -> None:
    side = tmp_path / "side"
    subprocess.run(["git", "init", "-q", "-b", "master", str(side)], check=True)
    (side / "side.txt").write_text("side\n", "utf-8")
    git(side, "add", "-A")
    git(side, "commit", "-qm", "Commit only in a bundle")
    bundle = tmp_path / "side.bundle"
    git(side, "bundle", "create", str(bundle), "master")
    side_head = git(side, "rev-parse", "HEAD")
    git(linked.folder, "config", "--local", "fetch.bundleURI", bundle.as_posix())
    linked.remote.edit("refs.bib", "Fixture Press", "Fixture Press Ltd", "Collaborator edit")
    assert linked.preview()["plan"]["kind"] == "fast_forward"
    assert not _present(linked.folder, side_head)
    assert git(linked.folder, "for-each-ref", "refs/bundles") == ""


def test_an_approved_push_never_sends_tags(linked: Linked) -> None:
    pushed = _committed(linked)
    git(linked.folder, "tag", "-a", "-m", "Local tag", "v-local-only", pushed)
    git(linked.folder, "config", "--local", "push.followTags", "true")
    linked.grant_and_push()
    assert linked.remote.head() == pushed
    assert git(linked.remote.bare, "tag", "--list") == ""


# ----- INT2-I1: every scope, and fail closed ------------------------------------------------------


def _leak(monkeypatch: pytest.MonkeyPatch, **variables: str) -> None:
    """Simulate a regression of the runner's environment whitelist."""
    original = gitsync.Git.environment

    def leaky(self: gitsync.Git) -> dict[str, str]:
        return {**original(self), **variables}

    monkeypatch.setattr(gitsync.Git, "environment", leaky)


def test_configuration_passed_through_the_environment_is_refused(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Command-scope entries are no longer skipped wholesale: only PrometheOS's own
    overrides may appear there, so configuration leaked through GIT_CONFIG_COUNT/KEY/VALUE
    (or GIT_CONFIG_PARAMETERS) is refused even if the whitelist regressed."""
    pushed = _committed(linked)
    decoy = _copy(linked, tmp_path)
    before = _heads(linked.remote.bare, decoy)
    _leak(
        monkeypatch,
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0=f"url.{decoy}.pushInsteadOf",
        GIT_CONFIG_VALUE_0=str(linked.remote.bare),
    )
    assert f"command:url.{MASKED}.pushinsteadof" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    assert _heads(linked.remote.bare, decoy) == before
    assert pushed not in before


def test_system_scope_redirects_are_refused(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = _copy(linked, tmp_path)
    system = tmp_path / "system.gitconfig"
    git(tmp_path, "config", "--file", str(system), "remote.origin.pushurl", str(decoy))
    _leak(monkeypatch, GIT_CONFIG_SYSTEM=str(system))
    assert "system:remote.origin.pushurl" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


@pytest.mark.parametrize("case", ["pushurl", "insteadof", "include"])
def test_non_local_push_urls_rewrites_and_includes_are_refused(
    linked: Linked, tmp_path: Path, profile: Path, case: str
) -> None:
    """Review INT2-I1: the any-scope refusal had local tests only (mutation M4 survived)."""
    decoy = _copy(linked, tmp_path)
    if case == "pushurl":
        _global(profile, "remote.origin.pushurl", str(decoy))
        expected = "global:remote.origin.pushurl"
    elif case == "insteadof":
        _global(profile, f"url.{decoy}.insteadOf", str(linked.remote.bare))
        expected = f"global:url.{MASKED}.insteadof"
    else:
        extra = profile / "extra.gitconfig"
        git(profile, "config", "--file", str(extra), f"url.{decoy}.pushInsteadOf", "x")
        _global(profile, "include.path", str(extra))
        expected = f"global:url.{MASKED}.pushinsteadof"
    assert expected in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


@pytest.mark.parametrize("failure", ["old-git", "malformed-listing"])
def test_configuration_that_cannot_be_listed_fails_closed(
    linked: Linked, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """71c244b treated a failing ``git config`` listing as empty (git older than 2.26 has
    no --show-scope), so nothing was refused."""
    real = gitsync.Git.run

    def broken(self: gitsync.Git, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("config", "--show-scope"):
            if failure == "old-git":
                return subprocess.CompletedProcess(args, 129, "", "unknown option show-scope")
            return subprocess.CompletedProcess(args, 0, "local\0core.bare\nfalse\0local", "")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(gitsync.Git, "run", broken)
    _refused(lambda: _status(linked))
    for action in _actions(linked):
        _refused(action)


def test_refused_settings_never_show_a_credential(
    linked: Linked, tmp_path: Path, offline: list[tuple[str, ...]]
) -> None:
    """Setting names can carry URLs (``url.<base>.*``, a remote named like a URL); the
    refusal lists them without the URL, so a token in one is never echoed."""
    decoy = _copy(linked, tmp_path)
    tokened = f"https://git:{TOKEN}@git.overleaf.com/0123456789abcdef"
    git(linked.folder, "config", "--local", f"url.{tokened}.insteadOf", "https://elsewhere/")
    git(linked.folder, "config", "--local", f"remote.{tokened}.url", str(decoy))
    status = _status(linked)
    assert f"local:url.{MASKED}.insteadof" in status["blocked_settings"]
    assert f"local:remote.{MASKED}.url" in status["blocked_settings"]
    errors = [_refused(action) for action in _actions(linked)]
    _no_network(offline)
    shown = str(status) + "".join(str(e.to_response().model_dump()) for e in errors)
    assert TOKEN.casefold() not in shown.casefold()


def test_a_carriage_return_cannot_hide_a_setting(linked: Linked) -> None:
    """Git accepts a carriage return inside a subsection name. Decoded with universal
    newlines, ``url.x<CR>.pushinsteadof`` read as the harmless name ``url.x``, so the rewrite
    escaped the refusal: the listing is now decoded exactly, and such a name is shown
    masked."""
    with (linked.folder / ".git" / "config").open("ab") as config:
        config.write(b'[url "x\r"]\n\tpushInsteadOf = elsewhere\n')
    assert f"local:url.{MASKED}.pushinsteadof" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


def test_a_redirect_added_after_the_preview_is_refused_before_the_push(
    linked: Linked, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration is checked again right before the push, after the capability is
    consumed, so an edit made while the preview fetched cannot steer the approved push."""
    pushed = _committed(linked)
    decoy = _copy(linked, tmp_path)
    before = _heads(linked.remote.bare, decoy)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    real = gitsync.capabilities.consume
    origin = _origin(linked)

    def consume_then_edit(*args: Any, **kwargs: Any) -> Any:
        done = real(*args, **kwargs)
        git(linked.folder, "config", "--local", f"remote.{origin}.url", str(decoy))
        return done

    monkeypatch.setattr(gitsync.capabilities, "consume", consume_then_edit)
    _refused(
        lambda: linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    )
    assert _heads(linked.remote.bare, decoy) == before
    assert pushed not in before


# ----- INT2-F3: the CLI confirmation names the destination ----------------------------------------


def test_cli_push_confirmation_names_the_destination(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed = _committed(linked)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    destination = preview["plan"]["remote_url"]
    assert destination == str(linked.remote.bare)
    monkeypatch.setenv("PROMETHEOS_HOME", str(linked.env.home))
    monkeypatch.delenv("PROMETHEOS_PUSH_CAPABILITY", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    real_token_hex = secrets.token_hex
    monkeypatch.setattr(
        cli.secrets, "token_hex", lambda n=None: "c0ffee" if n == 3 else real_token_hex(n)
    )
    command = ["paper", "overleaf", "push", linked.paper_id, "--digest", preview["digest"]]
    command += ["--project", linked.pid, "--json"]
    refused = CliRunner().invoke(cli.app, command, input="nope\n")
    assert refused.exit_code != 0 and linked.remote.head() != pushed
    assert f"Destination: {destination}" in refused.output
    result = CliRunner().invoke(cli.app, command, input="c0ffee\n")
    assert result.exit_code == 0, result.output
    shown = result.output.index(f"Destination: {destination}")
    assert shown < result.output.index("To push exactly these commits, type c0ffee")
    assert linked.remote.head() == pushed


def test_these_regressions_run_in_the_c09_checks_that_claim_them() -> None:
    """Review int1 F3: a check that claims a property must run the test of that property."""
    suites = json.loads(
        (Path(__file__).resolve().parents[1] / "tools" / "checkpoint_suites.json").read_text(
            encoding="utf-8"
        )
    )["suites"]["CP9"]
    for check in ("C09-SYNC", "C09-SAFETY"):
        files = {part for cmd in suites[check]["commands"].values() for part in cmd}
        assert "tests/test_cp9_integration_fixes_r2.py" in files, check


# ----- pre-check round 2 (development/receipts/CP9/review-precheck-int2-git) -------------------


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _append(config: Path, content: str) -> None:
    with config.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _local_config(linked: Linked) -> Path:
    return linked.folder / ".git" / "config"


def _decoy_in_clone(linked: Linked) -> tuple[str, Path]:
    """A copy of the approved repository at the place a rewrite that removes the URL's
    folder prefix would name (git resolves the remainder in the clone folder). Returns that
    prefix and the copy."""
    origin = _origin(linked)
    suffix = linked.remote.bare.name
    assert origin.endswith(suffix), origin
    decoy = linked.folder / suffix
    shutil.copytree(linked.remote.bare, decoy)
    info = linked.folder / ".git" / "info"
    info.mkdir(exist_ok=True)
    _append(info / "exclude", f"/{suffix}/\n")
    return origin[: -len(suffix)], decoy


@pytest.mark.parametrize("scope", ["local", "global"])
def test_a_url_rewrite_with_an_empty_base_is_refused(
    linked: Linked, profile: Path, scope: str
) -> None:
    """INT2G-F1: the refusal matched ``url.<base>.*`` only with a non-empty base, so a
    rewrite with an empty base passed every check while git applied it; on d11217d the
    approved push landed elsewhere and was reported as done."""
    pushed = _committed(linked)
    prefix, decoy = _decoy_in_clone(linked)
    before = _heads(linked.remote.bare, decoy)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    config = _local_config(linked) if scope == "local" else profile / ".gitconfig"
    _append(config, f'[url ""]\n\tpushInsteadOf = {_quoted(prefix)}\n')
    assert f'{scope}:url."".pushinsteadof' in _status(linked)["blocked_settings"]
    _refused(
        lambda: linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    )
    for action in _actions(linked):
        _refused(action)
    assert _heads(linked.remote.bare, decoy) == before
    assert pushed not in before


def test_an_empty_base_fetch_rewrite_is_refused_before_any_fetch(
    linked: Linked, offline: list[tuple[str, ...]]
) -> None:
    """INT2G-F1, fetch side: on d11217d the pull preview presented another repository's
    commit as the approved URL's."""
    prefix, _ = _decoy_in_clone(linked)
    _append(_local_config(linked), f'[url ""]\n\tinsteadOf = {_quoted(prefix)}\n')
    assert 'local:url."".insteadof' in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    _no_network(offline)


def test_link_refuses_an_empty_base_rewrite_before_it_clones(tmp_path: Path, profile: Path) -> None:
    env, remote, paper_id = _unlinked(tmp_path)
    prefix = str(remote.bare)[: -len(remote.bare.name)]
    _append(profile / ".gitconfig", f'[url ""]\n\tinsteadOf = {_quoted(prefix)}\n')
    with pytest.raises(PrometheosError) as refused:
        env.svc.overleaf_link(
            env.project_id, OWNER, paper_id, str(remote.bare), "research", "overleaf"
        )
    assert refused.value.code == "policy_denied", refused.value
    assert not (env.research / "overleaf").exists()


@pytest.mark.parametrize(
    ("entry", "shown"),
    [
        ('[url ""]\n\tinsteadOf = "zz"\n', 'local:url."".insteadof'),
        ('[remote ""]\n\tpushurl = "zz"\n', 'local:remote."".pushurl'),
        ('[remote ""]\n\tuploadpack = "zz"\n', 'local:remote."".uploadpack'),
        ('[diff ""]\n\tcommand = zz\n', 'diff."".command'),
        ('[merge ""]\n\tdriver = zz\n', 'merge."".driver'),
        ('[http ""]\n\tsslVerify = false\n', 'http."".sslverify'),
    ],
)
def test_settings_with_an_empty_subsection_are_matched_and_shown(
    linked: Linked, entry: str, shown: str
) -> None:
    """Refusals now match the parsed section, subsection and key, so an empty subsection
    is matched like any other and shown visibly."""
    _append(_local_config(linked), entry)
    assert shown in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


def test_a_push_that_misses_the_approved_destination_is_not_reported_as_done(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth (INT2G-F1): with the every-scope refusal disabled, a push-only
    rewrite sends the commits elsewhere. The check after the push finds that the approved
    destination does not hold the pushed commit and reports an unknown outcome."""
    pushed = _committed(linked)
    prefix, decoy = _decoy_in_clone(linked)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    _append(_local_config(linked), f'[url ""]\n\tpushInsteadOf = {_quoted(prefix)}\n')
    monkeypatch.setattr(gitsync.Git, "redirects", lambda self, entries=None: [])
    with pytest.raises(PrometheosError) as unknown:
        linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    assert unknown.value.code == "io_error", unknown.value
    assert unknown.value.details["outcome"] == "unknown"
    assert linked.remote.head() != pushed
    assert git(decoy, "rev-parse", "refs/heads/master") == pushed


@pytest.mark.parametrize("scope", ["local", "global"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("fetch", "+refs/heads/master:refs/remotes/origin/master"),
        ("proxy", "http://127.0.0.1:9"),
        ("mirror", "true"),
    ],
)
def test_every_key_of_a_remote_named_like_the_destination_is_refused(
    linked: Linked,
    profile: Path,
    offline: list[tuple[str, ...]],
    scope: str,
    key: str,
    value: str,
) -> None:
    """INT2G-F3: git consults a remote named like the URL even without a url key; the
    refusal covers every key (the reviewer's narrowing mutant survived the earlier tests)."""
    name = f"remote.{_origin(linked)}.{key}"
    if scope == "local":
        git(linked.folder, "config", "--local", name, value)
    else:
        _global(profile, name, value)
    assert f"{scope}:remote.{MASKED}.{key}" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)
    _no_network(offline)


@pytest.mark.parametrize(
    "url",
    [
        "https://git.overleaf.com/0123\u202eabc",  # right-to-left override (format)
        "https://git.overleaf.com/0123\u200babc",  # zero-width space (format)
        "https://git.overleaf.com/0123\u0085abc",  # C1 control
        "https://git.overleaf.com/0123\u00a0abc",  # no-break space
        "https://git.\u043everleaf.com/0123",  # a Cyrillic letter in the host
        "https://git.overleaf.com/caf\u00e9",  # any non-ASCII character in a URL
    ],
)
def test_urls_with_invisible_or_non_ascii_characters_are_refused(url: str) -> None:
    """INT2G-F2: the destination shown at approval must read as what it is."""
    with pytest.raises(PrometheosError) as refused:
        gitsync.check_remote_url(url)
    assert refused.value.code == "policy_denied"
    assert url not in str(refused.value.to_response().model_dump())


def test_ascii_hosts_and_letters_in_local_paths_stay_accepted(tmp_path: Path) -> None:
    punycode = "https://xn--vrleaf-9ta.example/0123456789abcdef"
    assert gitsync.check_remote_url(punycode) == punycode
    lettered = tmp_path / "Jos\u00e9" / "paper.git"
    lettered.mkdir(parents=True)
    assert gitsync.check_remote_url(str(lettered)) == str(lettered)
    hidden = tmp_path / "pa\u200bper.git"
    hidden.mkdir()
    with pytest.raises(PrometheosError):
        gitsync.check_remote_url(str(hidden))


def test_a_destination_with_an_invisible_character_is_not_accepted_or_echoed(
    linked: Linked, offline: list[tuple[str, ...]]
) -> None:
    git(linked.folder, "remote", "set-url", "origin", "https://git.overleaf.com/0123\u202eabc")
    status = _status(linked)
    assert status["remote"]["url"] is None and status["remote"]["url_accepted"] is False
    errors = [_refused(action) for action in _actions(linked)]
    _no_network(offline)
    shown = str(status) + "".join(str(e.to_response().model_dump()) for e in errors)
    assert "\u202e" not in shown


def test_destination_lines_escape_invisible_characters() -> None:
    """Defence in depth for the CLI line (the hub card does the same): any control, format,
    separator or unassigned character is shown as an escape."""
    assert text.visible("a\u202eb\u200bc d\u0085") == "a\\u{202e}b\\u{200b}c d\\u{85}"
    assert text.visible("C:\\Users\\Jos\u00e9\\paper.git") == "C:\\Users\\Jos\u00e9\\paper.git"


def test_the_advice_for_several_origin_urls_works(
    linked: Linked, tmp_path: Path, profile: Path
) -> None:
    """INT2G-F4: the entry names the scopes, and the advised commands, run in each scope
    listed, leave exactly one URL (``git remote set-url`` alone fails with two values)."""
    origin = _origin(linked)
    other = _copy(linked, tmp_path, "other.git")
    git(linked.folder, "remote", "set-url", "--add", "origin", str(other))
    _global(profile, "remote.origin.url", str(other))
    status = _status(linked)
    assert (
        "remote.origin.url (3 values in global, local; keep exactly one)"
        in status["blocked_settings"]
    )
    message = _refused(
        lambda: linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    ).message
    assert "--unset-all" in message and "git remote set-url origin" in message
    git(linked.folder, "config", "--local", "--unset-all", "remote.origin.url")
    _global(profile, "--unset-all", "remote.origin.url")
    git(linked.folder, "remote", "set-url", "origin", origin)
    assert _status(linked)["blocked_settings"] == []


@pytest.mark.parametrize("version", [(2, 44, 2), (2, 41, 0), None])
def test_git_older_than_the_supported_floor_is_refused(
    linked: Linked, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] | None
) -> None:
    """INT2G-F5: every call needs --attr-source (git 2.41), merge previews need 2.43, and
    the status view relies on GIT_NO_LAZY_FETCH (2.45). An unknown version is refused too."""
    monkeypatch.setattr(gitsync, "_git_version", lambda executable: version)
    with pytest.raises(PrometheosError) as refused:
        _status(linked)
    assert refused.value.code == "unsupported"
    assert "2.45" in refused.value.message


def test_the_installed_git_meets_the_floor(linked: Linked, monkeypatch: pytest.MonkeyPatch) -> None:
    assert gitsync._git_version(gitsync.Git(linked.folder, linked.env.home).executable) >= (
        2,
        45,
        0,
    )
    monkeypatch.setattr(gitsync, "_git_version", lambda executable: (2, 45, 0))
    assert _status(linked)["blocked_settings"] == []


def test_attributes_are_read_where_git_reads_them(linked: Linked, tmp_path: Path) -> None:
    """INT2G-I1 (a): git finds info/attributes through the repository's common directory,
    which a commondir file can place elsewhere; the refusal now asks git for that path."""
    common = tmp_path / "common.git"
    shutil.copytree(linked.folder / ".git", common)
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "attributes").write_text("* merge=marker\n", "utf-8")
    (linked.folder / ".git" / "commondir").write_text(f"{common}\n", "utf-8")
    assert "info/attributes (filter/diff/merge drivers)" in _status(linked)["blocked_settings"]
    for action in _actions(linked):
        _refused(action)


@pytest.mark.parametrize(
    ("scope", "key", "value", "shown"),
    [
        ("local", "gc.recentObjectsHook", "prometheos-canary", "gc.recentobjectshook"),
        ("global", "promisor.acceptFromServer", "all", "global:promisor.acceptfromserver"),
    ],
)
def test_two_further_settings_are_listed(
    linked: Linked, profile: Path, scope: str, key: str, value: str, shown: str
) -> None:
    """INT2G-I1 (b, c): both were neutralised already (no gc runs; no lazy fetch); they are
    now refused by name as well."""
    if scope == "local":
        git(linked.folder, "config", "--local", key, value)
    else:
        _global(profile, key, value)
    assert shown in _status(linked)["blocked_settings"]


def test_a_check_reads_the_configuration_once(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INT2G-I1 (d): one listing per check (it was three), so the analyses see one
    snapshot."""
    listings: list[tuple[str, ...]] = []
    real = gitsync.Git.run

    def counting(self: gitsync.Git, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("config", "--show-scope"):
            listings.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(gitsync.Git, "run", counting)
    _status(linked)
    assert len(listings) == 1


# ----- pre-check round 3 (development/receipts/CP9/review-precheck-int2-git-r2) ----------------

RLO, SOH, BS = chr(0x202E), chr(1), chr(92)


def _shown_as(code: int) -> str:
    """How an invisible character appears once escaped (``\\u{hex}``)."""
    return f"{BS}u{{{code:x}}}"


def test_cli_push_confirmation_escapes_every_agent_written_line(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-L1: the confirmation printed commit subjects (agents write them through
    overleaf_commit) and the branch name raw; every line of an owner prompt is now
    escaped."""
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, AGENT, linked.paper_id, f"Fix {RLO}evil{SOH} end")
    git(linked.folder, "branch", "-m", "master", f"master{RLO}")
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    monkeypatch.setenv("PROMETHEOS_HOME", str(linked.env.home))
    monkeypatch.delenv("PROMETHEOS_PUSH_CAPABILITY", raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    real_token_hex = secrets.token_hex
    monkeypatch.setattr(
        cli.secrets, "token_hex", lambda n=None: "c0ffee" if n == 3 else real_token_hex(n)
    )
    command = ["paper", "overleaf", "push", linked.paper_id, "--digest", preview["digest"]]
    result = CliRunner().invoke(cli.app, [*command, "--project", linked.pid], input="nope\n")
    assert result.exit_code != 0
    assert f"branch master{_shown_as(0x202E)} (no force)" in result.output
    assert f"Fix {_shown_as(0x202E)}evil{_shown_as(1)} end" in result.output
    assert RLO not in result.output and SOH not in result.output


def test_cli_json_output_escapes_invisible_characters_losslessly(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-L1, pull side: the preview JSON printed collaborator-written subjects raw. They are
    now JSON escapes, so a reader gets the same value and the terminal no bidi control."""
    subject = f"Collaborator {RLO}edit"
    linked.remote.edit("refs.bib", "Fixture Press", "Fixture Press Ltd", subject)
    monkeypatch.setenv("PROMETHEOS_HOME", str(linked.env.home))
    command = ["paper", "overleaf", "pull", linked.paper_id, "--project", linked.pid, "--json"]
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    assert RLO not in result.stdout
    payload = json.loads(result.stdout)
    assert [c["subject"] for c in payload["plan"]["incoming_commits"]] == [subject]


def _another_computer(value: object) -> bool:
    if not isinstance(value, (str, os.PathLike)):
        return False
    named = os.fspath(value)
    if not isinstance(named, str):
        return False
    lowered = named.lower()
    return (
        re.match(r"^[\\/]{2}", named) is not None
        or lowered.startswith("file:")
        or "server" in lowered
        or "localhost" in lowered
    )


@pytest.fixture
def no_share_access(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
    """Returns ``activate()``: from then on, a filesystem check or a git command that would
    reach a path or URL naming another computer fails the test before anything is touched
    (no real share is ever contacted)."""
    touched: list[str] = []

    def guard(real: Any) -> Any:
        """Wraps a function or method whose first argument is the path it touches."""

        def guarded(*args: Any, **kwargs: Any) -> Any:
            target = args[0] if args else None
            if _another_computer(target):
                touched.append(os.fspath(target))  # type: ignore[arg-type]
                raise AssertionError(f"a network path was about to be touched: {target!r}")
            return real(*args, **kwargs)

        return guarded

    def activate() -> list[str]:
        for owner, name in (
            (pathlib.Path, "is_dir"),
            (pathlib.Path, "exists"),
            (pathlib.Path, "stat"),
            (os, "stat"),
            (os.path, "isdir"),
            (os.path, "exists"),
        ):
            monkeypatch.setattr(owner, name, guard(getattr(owner, name)))
        real_run = gitsync.Git.run

        def run(self: gitsync.Git, *args: str, **kwargs: Any) -> Any:
            if any(_another_computer(arg) for arg in args):
                raise AssertionError(f"a git command would reach a network path: {args!r}")
            return real_run(self, *args, **kwargs)

        monkeypatch.setattr(gitsync.Git, "run", run)
        return touched

    return activate


NETWORK_DESTINATIONS = [
    f"{BS}{BS}server{BS}share{BS}paper.git",
    "//server/share/paper.git",
    f"{BS}{BS}?{BS}UNC{BS}server{BS}share{BS}paper.git",
    f"{BS}{BS}?{BS}C:{BS}papers{BS}paper.git",
    f"{BS}{BS}.{BS}C:{BS}papers{BS}paper.git",
    f"/{BS}server{BS}share{BS}paper.git",
    "file://server/share/paper.git",
    "file://localhost/C:/papers/paper.git",
    "file:////server/share/paper.git",
    "file:///%5C%5Cserver/share/paper.git",
]


@pytest.mark.parametrize("url", NETWORK_DESTINATIONS)
def test_network_destinations_are_refused_before_any_access(
    url: str, no_share_access: Callable[[], list[str]]
) -> None:
    """R2-L2: UNC and device paths, and file URLs with a host or a network path, were
    accepted as local; checking or using one reaches another computer (Windows may sign
    in on its own). They are refused before any filesystem or git access."""
    touched = no_share_access()
    with pytest.raises(PrometheosError) as refused:
        gitsync.check_remote_url(url)
    assert refused.value.code == "policy_denied"
    assert url not in str(refused.value.to_response().model_dump())
    assert touched == []


def test_local_drive_destinations_stay_accepted(
    tmp_path: Path, no_share_access: Callable[[], list[str]]
) -> None:
    no_share_access()
    repository = tmp_path / "paper.git"
    repository.mkdir()
    assert gitsync.check_remote_url(str(repository)) == str(repository)
    file_url = "file:///" + repository.as_posix()
    assert gitsync.check_remote_url(file_url) == file_url


def test_link_refuses_a_network_share_before_any_access(
    tmp_path: Path, no_share_access: Callable[[], list[str]]
) -> None:
    env, _remote, paper_id = _unlinked(tmp_path)
    touched = no_share_access()
    with pytest.raises(PrometheosError) as refused:
        env.svc.overleaf_link(
            env.project_id, OWNER, paper_id, NETWORK_DESTINATIONS[0], "research", "overleaf"
        )
    assert refused.value.code == "policy_denied"
    assert touched == []
    assert not (env.research / "overleaf").exists()


@pytest.mark.parametrize("url", [NETWORK_DESTINATIONS[0], "file://server/share/paper.git"])
def test_a_network_share_origin_is_refused_before_any_access(
    linked: Linked, no_share_access: Callable[[], list[str]], url: str
) -> None:
    git(linked.folder, "remote", "set-url", "origin", url)
    touched = no_share_access()
    status = _status(linked)
    assert status["remote"]["url"] is None and status["remote"]["url_accepted"] is False
    for action in _actions(linked):
        _refused(action)
    assert touched == []


def test_a_failed_check_after_the_push_is_an_unknown_outcome(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-I2 (b): when the fetch after a push fails, the push may have happened; it is
    reported as an unknown outcome, like a failed check, never as done."""
    pushed = _committed(linked)
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    real = gitsync._fetch
    calls: list[str] = []

    def failing_after_the_push(runner: gitsync.Git, url: str) -> None:
        calls.append(url)
        if len(calls) > 1:
            raise PrometheosError("io_error", "git fetch failed", {"exit": 128, "stderr": "x"})
        real(runner, url)

    monkeypatch.setattr(gitsync, "_fetch", failing_after_the_push)
    with pytest.raises(PrometheosError) as unknown:
        linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    assert unknown.value.code == "io_error"
    assert unknown.value.details["outcome"] == "unknown"
    assert linked.remote.head() == pushed


def test_the_advice_covers_a_url_from_an_included_file(
    linked: Linked, tmp_path: Path, profile: Path
) -> None:
    """R2-I1 (c): ``--<scope> --unset-all`` cannot remove a value that comes from an
    included file (git exits 5); the refusal names the command that finds the file and the
    one that removes the value there."""
    other = _copy(linked, tmp_path, "other.git")
    extra = profile / "extra.gitconfig"
    git(profile, "config", "--file", str(extra), "remote.origin.url", str(other))
    _global(profile, "include.path", str(extra))
    blocked = _status(linked)["blocked_settings"]
    assert "remote.origin.url (2 values in global, local; keep exactly one)" in blocked
    message = _refused(
        lambda: linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    ).message
    assert "--show-origin" in message and "--file <that file> --unset-all" in message
    scope_only = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(profile / ".gitconfig"),
            "--unset-all",
            "remote.origin.url",
        ],
        capture_output=True,
        check=False,
    )
    assert scope_only.returncode == 5
    git(profile, "config", "--file", str(extra), "--unset-all", "remote.origin.url")
    assert _status(linked)["blocked_settings"] == []
