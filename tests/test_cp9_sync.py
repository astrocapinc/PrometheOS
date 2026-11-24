"""C09-SYNC (S21) and the Git half of C09-SAFETY (S22) against a local bare repository.

The bare repository stands in for Overleaf; a second clone plays the collaborator. No
network is used and no credential is involved.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import msys_retry
import pytest
from cp1_support import AGENT, OWNER, Env
from cp7_support import anchor_body, create
from cp9_support import LATEX_FIXTURE

from prometheos.app import gitsync
from prometheos.domain.ids import new_id
from prometheos.domain.models import PrometheosError

IDENTITY = ["-c", "user.name=Collaborator", "-c", "user.email=collab@example.invalid"]


def application_git_environment() -> dict[str, str]:
    """The environment the application gives git (``gitsync.Git.environment``), read now.

    Test-side git uses the application's policy, so both sides read the same configuration
    files: system and global from their standard locations, never configuration named or
    suppressed by GIT_CONFIG_* variables, which a launcher may set for its own isolation and
    which the application deliberately ignores (CP9 F1, INT2-I1). With differing policies a
    host setting such as ``core.autocrlf`` applies to one side only, and the two disagree
    about whether a work file matches the index (decision 0027). ``environment`` depends only
    on the git executable, so no runner (with its neutral tool folders) is constructed."""
    runner = object.__new__(gitsync.Git)
    runner.executable = shutil.which("git") or "git"
    return runner.environment()


def git(cwd: Path, *args: str) -> str:
    # Fixture git only: retried when the MSYS runtime aborts at start-up (decision 0037).
    done = msys_retry.run(
        ["git", *IDENTITY, *args], cwd=cwd, env=application_git_environment(), text=True
    )
    return str(done.stdout).strip()


class Remote:
    """A bare 'Overleaf' repository seeded with the fixture and a collaborator clone."""

    def __init__(self, tmp: Path) -> None:
        self.bare = tmp / "overleaf.git"
        seed = tmp / "seed"
        env = application_git_environment()
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "master", str(self.bare)], env=env, check=True
        )
        subprocess.run(["git", "init", "-q", "-b", "master", str(seed)], env=env, check=True)
        for path in LATEX_FIXTURE.rglob("*"):
            if path.is_file():
                target = seed / path.relative_to(LATEX_FIXTURE)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "Initial Overleaf project")
        git(seed, "remote", "add", "origin", str(self.bare))
        git(seed, "push", "-q", "origin", "master")
        self.collab = tmp / "collaborator"
        msys_retry.run(
            ["git", "-c", "core.autocrlf=false", "clone", "-q", str(self.bare), str(self.collab)],
            env=env,
        )

    def edit(self, rel: str, old: str, new: str, message: str) -> str:
        path = self.collab / rel
        text = path.read_text("utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), "utf-8", newline="\n")
        git(self.collab, "commit", "-qam", message)
        git(self.collab, "push", "-q", "origin", "master")
        return git(self.collab, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.bare, "rev-parse", "refs/heads/master")


class Linked:
    def __init__(self, tmp: Path) -> None:
        self.env = Env(tmp)
        self.svc = self.env.svc
        self.pid = self.env.project_id
        self.remote = Remote(tmp)
        from cp7_support import paper_body

        self.paper_id = new_id("paper")
        self.env.apply([create("paper", paper_body(title="Linked paper"), self.paper_id)])
        self.linked = self.svc.overleaf_link(
            self.pid, OWNER, self.paper_id, str(self.remote.bare), "research", "overleaf"
        )
        self.folder = self.env.research / "overleaf"

    def local_edit(self, rel: str, old: str, new: str) -> None:
        current = self.svc.workspace_read(self.pid, OWNER, self.paper_id, rel)
        assert old in current["text"]
        self.svc.workspace_write(
            self.pid,
            OWNER,
            self.paper_id,
            rel,
            current["text"].replace(old, new, 1),
            current["sha256"],
        )

    def preview(self) -> dict[str, Any]:
        return self.svc.overleaf_pull_preview(self.pid, OWNER, self.paper_id)

    def pull(self) -> dict[str, Any]:
        preview = self.preview()
        return self.svc.overleaf_pull_apply(self.pid, OWNER, self.paper_id, preview["digest"])

    def grant_and_push(self) -> dict[str, Any]:
        preview = self.svc.overleaf_push_preview(self.pid, OWNER, self.paper_id)
        grant = self.svc.overleaf_push_grant(self.pid, preview["digest"], "SYNTHETIC-TEST push")
        return self.svc.overleaf_push(
            self.pid, OWNER, self.paper_id, preview["digest"], grant.secret
        )


@pytest.fixture
def linked(tmp_path: Path) -> Linked:
    return Linked(tmp_path)


# ----- link, fast-forward and clean merge ---------------------------------------------------


def test_link_clones_and_binds_the_workspace(linked: Linked) -> None:
    paper = linked.svc.get(linked.pid, OWNER, linked.paper_id, None)
    source = linked.svc.get(linked.pid, OWNER, paper["body"]["manuscript_source_id"], None)
    assert source["body"]["root_binding"] == {"alias": "research", "path": "overleaf/main.tex"}
    status = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert status["branch"] == "master" and status["ahead"] == 0 and status["behind"] == 0
    assert status["remote"]["url_accepted"] is True and status["blocked_settings"] == []
    assert any("comments" in item for item in status["limitations"])


def test_fast_forward_pull_previews_then_integrates(linked: Linked) -> None:
    head = linked.remote.edit(
        "sections/intro.tex", "Nothing in it", "Still nothing in it", "Collaborator edit"
    )
    preview = linked.preview()
    plan = preview["plan"]
    assert plan["kind"] == "fast_forward" and preview["applicable"]
    assert plan["incoming_head"] == head
    assert [c["subject"] for c in plan["incoming_commits"]] == ["Collaborator edit"]
    assert plan["incoming_files"] == [{"status": "M", "path": "sections/intro.tex"}]
    # Previewing changed nothing locally.
    assert "Still nothing" not in (linked.folder / "sections/intro.tex").read_text("utf-8")
    result = linked.svc.overleaf_pull_apply(linked.pid, OWNER, linked.paper_id, preview["digest"])
    assert result["applied"] == "fast_forward" and result["head"] == head
    assert "Still nothing" in (linked.folder / "sections/intro.tex").read_text("utf-8")
    # The pulled file is recorded as a new version.
    version_id = result["snapshot"]["versions"]["sections/intro.tex"]["version_id"]
    assert linked.svc.get(linked.pid, OWNER, version_id, None)["kind"] == "version"


def test_clean_merge_of_non_overlapping_changes(linked: Linked) -> None:
    linked.local_edit("main.tex", "Synthetic Workspace Paper", "Synthetic Workspace Paper v2")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Local title change")
    linked.remote.edit("refs.bib", "Fixture Press", "Fixture Press Ltd", "Collaborator bib edit")
    preview = linked.preview()
    assert preview["plan"]["kind"] == "merge" and preview["applicable"]
    assert preview["plan"]["conflicts"] == []
    result = linked.svc.overleaf_pull_apply(linked.pid, OWNER, linked.paper_id, preview["digest"])
    assert result["applied"] == "merge"
    assert "v2" in (linked.folder / "main.tex").read_text("utf-8")
    assert "Fixture Press Ltd" in (linked.folder / "refs.bib").read_text("utf-8")
    parents = git(linked.folder, "log", "-1", "--format=%P").split()
    assert len(parents) == 2


def tree_state(folder: Path) -> dict[str, Any]:
    """The clone's exact work-file and index bytes, HEAD and refs, for before/after checks."""
    files = {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(folder).parts
    }
    refs = git(folder, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    return {
        "files": files,
        "index": (folder / ".git" / "index").read_bytes(),
        "HEAD": (folder / ".git" / "HEAD").read_bytes(),
        "local_refs": [r for r in refs if not r.startswith("refs/remotes/")],
        "remote_refs": [r for r in refs if r.startswith("refs/remotes/")],
    }


def unclean(folder: Path) -> list[str]:
    """Tracked paths whose work file, as git would store it here, is not the indexed blob.

    Unlike ``git status`` this never trusts the index's stat cache, so a racily clean entry
    cannot decide the answer: the work file's content is compared under the test-side Git
    configuration, which must be the application's (decision 0027)."""
    staged = [e for e in git(folder, "ls-files", "-s", "-z").split("\0") if e]
    indexed = {path: meta.split()[1] for meta, path in (e.split("\t", 1) for e in staged)}
    paths = sorted(indexed)
    stored = git(folder, "hash-object", "--", *paths).splitlines()
    return [p for p, oid in zip(paths, stored, strict=True) if indexed[p] != oid]


def test_divergence_stops_with_both_sides_intact(linked: Linked) -> None:
    linked.local_edit("main.tex", "settling time", "rise time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Local wording")
    local_head = git(linked.folder, "rev-parse", "HEAD")
    remote_head = linked.remote.edit("main.tex", "settling time", "overshoot", "Remote wording")
    assert unclean(linked.folder) == []  # the committed clone matches its index
    before = tree_state(linked.folder)
    preview = linked.preview()
    assert preview["plan"]["kind"] == "diverged_conflict" and not preview["applicable"]
    assert preview["plan"]["conflicts"] == ["main.tex"]
    fetched = tree_state(linked.folder)
    with pytest.raises(PrometheosError) as refused:
        linked.svc.overleaf_pull_apply(linked.pid, OWNER, linked.paper_id, preview["digest"])
    assert refused.value.code == "revision_conflict"
    after = tree_state(linked.folder)
    # Byte for byte: neither the preview nor the refused apply changed a work file, the
    # index, HEAD or a local branch; the preview's fetch moved only remote-tracking refs, to
    # the collaborator's commit, and the refusal left them there.
    assert fetched == before | {"remote_refs": fetched["remote_refs"]}
    assert after == fetched
    assert f"refs/remotes/origin/master {remote_head}" in fetched["remote_refs"]
    assert f"refs/remotes/origin/master {remote_head}" not in before["remote_refs"]
    assert git(linked.folder, "rev-parse", "HEAD") == local_head
    assert "rise time" in (linked.folder / "main.tex").read_text("utf-8")
    assert unclean(linked.folder) == []
    assert git(linked.folder, "status", "--porcelain") == ""
    assert linked.remote.head() == remote_head
    # Nothing can be pushed over it either: the push plan says pull first.
    push = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert push["plan"]["kind"] == "blocked_behind" and not push["applicable"]


def test_local_uncommitted_edits_overlapping_incoming_block_the_pull(linked: Linked) -> None:
    linked.local_edit("sections/intro.tex", "second file", "2nd file")
    linked.remote.edit("sections/intro.tex", "Nothing in it", "Nothing here", "Remote intro edit")
    preview = linked.preview()
    assert preview["plan"]["kind"] == "blocked_local_changes"
    assert preview["plan"]["conflicts"] == ["sections/intro.tex"]
    assert "2nd file" in (linked.folder / "sections/intro.tex").read_text("utf-8")


def test_stale_pull_digest_is_refused(linked: Linked) -> None:
    linked.remote.edit("refs.bib", "Fixture Press", "Fixture Press Two", "One")
    preview = linked.preview()
    linked.remote.edit("refs.bib", "Fixture Press Two", "Fixture Press Three", "Two")
    linked.svc.overleaf_pull_preview(linked.pid, OWNER, linked.paper_id)  # fetches "Two"
    with pytest.raises(PrometheosError) as stale:
        linked.svc.overleaf_pull_apply(linked.pid, OWNER, linked.paper_id, preview["digest"])
    assert stale.value.code == "revision_conflict"


# ----- push -----------------------------------------------------------------------------------


def test_push_needs_the_exact_plan_capability_and_never_forces(linked: Linked) -> None:
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Hyphenate")
    before = linked.remote.head()
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    assert preview["plan"]["kind"] == "fast_forward_push" and preview["plan"]["force"] is False
    assert [c["subject"] for c in preview["plan"]["commits"]] == ["Hyphenate"]
    for secret in (None, "pcap_wrong"):
        with pytest.raises(PrometheosError) as denied:
            linked.svc.overleaf_push(linked.pid, AGENT, linked.paper_id, preview["digest"], secret)
        assert denied.value.code == "policy_denied"
    assert linked.remote.head() == before
    # A capability for another plan does not cover this one.
    other = linked.svc.overleaf_push_grant(linked.pid, "0" * 64, "SYNTHETIC-TEST other plan")
    with pytest.raises(PrometheosError):
        linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], other.secret
        )
    assert linked.remote.head() == before
    pushed = linked.grant_and_push()
    local = git(linked.folder, "rev-parse", "HEAD")
    assert pushed["remote_head"] == local == linked.remote.head()
    # The remote history still contains the previous head (fast-forward, nothing rewritten).
    assert git(linked.remote.bare, "merge-base", "--is-ancestor", before, local) == ""


def test_push_plan_changes_invalidate_the_capability(linked: Linked) -> None:
    linked.local_edit("main.tex", "settling time", "settling-time")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "First")
    preview = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    grant = linked.svc.overleaf_push_grant(linked.pid, preview["digest"], "SYNTHETIC-TEST push")
    linked.local_edit("main.tex", "settling-time", "settling time again")
    linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "Second")
    before = linked.remote.head()
    with pytest.raises(PrometheosError) as changed:
        linked.svc.overleaf_push(
            linked.pid, OWNER, linked.paper_id, preview["digest"], grant.secret
        )
    assert changed.value.code == "revision_conflict"
    assert linked.remote.head() == before
    # A remote that moved after the preview is refused the same way (no force).
    fresh = linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id)
    linked.remote.edit("refs.bib", "Fixture Press", "Other Press", "Concurrent remote")
    grant2 = linked.svc.overleaf_push_grant(linked.pid, fresh["digest"], "SYNTHETIC-TEST push")
    with pytest.raises(PrometheosError):
        linked.svc.overleaf_push(linked.pid, OWNER, linked.paper_id, fresh["digest"], grant2.secret)
    assert "Other Press" in (linked.remote.collab / "refs.bib").read_text("utf-8")


# ----- anchors follow the remap rules after pull -----------------------------------------------


def test_anchors_follow_remap_rules_after_pull(linked: Linked) -> None:
    paper = linked.svc.get(linked.pid, OWNER, linked.paper_id, None)
    source_id = paper["body"]["manuscript_source_id"]
    versions = linked.svc.list_records(linked.pid, OWNER, kind="version", limit=200)["items"]
    main_version = next(v for v in versions if v["body"]["source_id"] == source_id)
    lines = (linked.folder / "main.tex").read_text("utf-8").splitlines()
    results_line = next(n for n, t in enumerate(lines, 1) if "settling time" in t)
    title_line = next(n for n, t in enumerate(lines, 1) if "\\title{" in t)
    moved = new_id("anchor")
    edited = new_id("anchor")
    linked.env.apply(
        [
            create("anchor", anchor_body(linked.paper_id, main_version["id"], results_line), moved),
            create("anchor", anchor_body(linked.paper_id, main_version["id"], title_line), edited),
        ]
    )
    # Collaborator inserts a line near the top and rewrites the title line.
    path = linked.remote.collab / "main.tex"
    text = path.read_text("utf-8").replace(
        "\\title{Synthetic Workspace Paper}", "% added by collaborator\n\\title{Renamed Paper}"
    )
    path.write_text(text, "utf-8", newline="\n")
    git(linked.remote.collab, "commit", "-qam", "Collaborator restructure")
    git(linked.remote.collab, "push", "-q", "origin", "master")
    result = linked.pull()
    new_version = result["snapshot"]["versions"]["main.tex"]["version_id"]
    anchor = linked.svc.get(linked.pid, OWNER, moved, None)["body"]
    assert anchor["manuscript_version_id"] == new_version
    assert anchor["locator"]["start_line"] == results_line + 1
    assert anchor["status"] == "current"
    review = linked.svc.get(linked.pid, OWNER, edited, None)["body"]
    assert review["status"] == "needs_review"


# ----- Git safety ------------------------------------------------------------------------------

TOKEN = "olp_SYNTHETICTOKEN0123456789abcdef"


@pytest.mark.parametrize(
    "url",
    [
        f"https://git:{TOKEN}@git.overleaf.com/abc123",
        f"https://{TOKEN}@git.overleaf.com/abc123",
        f"https://git.overleaf.com/abc123?token={TOKEN}",
        f"https://git.overleaf.com/abc123#{TOKEN}",
        "ssh://git@git.overleaf.com/abc123",
        "git@github.com:someone/repo.git",
        "ext::sh -c touch% /tmp/pwned",
        "http://git.overleaf.com/abc123",
        "git://git.overleaf.com/abc123",
        "relative/path",
    ],
)
def test_credential_bearing_and_unsafe_urls_are_refused(tmp_path: Path, url: str) -> None:
    env = Env(tmp_path)
    from cp7_support import paper_body

    paper_id = new_id("paper")
    env.apply([create("paper", paper_body(), paper_id)])
    with pytest.raises(PrometheosError) as refused:
        env.svc.overleaf_link(env.project_id, OWNER, paper_id, url, "research", "clone")
    assert refused.value.code == "policy_denied"
    assert TOKEN not in str(refused.value.to_response().model_dump())
    assert not (env.research / "clone").exists()
    # Nothing about the URL reached the store.
    for path in env.store_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


HOSTILE_CONFIG = [
    ("core.fsmonitor", "sh -c 'echo fsmonitor > ../marker-fsmonitor'"),
    ("core.hooksPath", "evil-hooks"),
    ("core.pager", "sh -c 'echo pager > ../marker-pager'"),
    ("core.askPass", "sh -c 'echo askpass > ../marker-askpass'"),
    ("credential.helper", f"!echo password={TOKEN}"),
    ("filter.evil.smudge", "sh -c 'echo smudge > ../marker-smudge'"),
    ("diff.evil.command", "sh -c 'echo diff > ../marker-diff'"),
    ("merge.evil.driver", "sh -c 'echo merge > ../marker-merge'"),
    ("core.sshCommand", "sh -c 'echo ssh > ../marker-ssh'"),
]


@pytest.mark.parametrize(("key", "value"), HOSTILE_CONFIG)
def test_repository_local_executable_config_is_refused(
    linked: Linked, key: str, value: str
) -> None:
    git(linked.folder, "config", "--local", key, value)
    status = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert key.lower() in status["blocked_settings"]
    for action in (
        lambda: linked.preview(),
        lambda: linked.svc.overleaf_push_preview(linked.pid, OWNER, linked.paper_id),
        lambda: linked.svc.overleaf_commit(linked.pid, OWNER, linked.paper_id, "x"),
    ):
        with pytest.raises(PrometheosError) as refused:
            action()
        assert refused.value.code == "policy_denied"
    assert not list(linked.env.research.glob("marker-*"))


def test_overrides_neutralise_executable_config_even_without_the_guard(linked: Linked) -> None:
    """Defence in depth: run real operations through the runner with every hostile setting
    present (bypassing the refusal) and check that no program ran."""
    folder = linked.folder
    hooks = folder / "evil-hooks"
    hooks.mkdir()
    for name in ("post-merge", "post-checkout", "pre-commit", "pre-push", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} > ../marker-hook-{name}\n", newline="\n")
        default = folder / ".git" / "hooks" / name
        default.parent.mkdir(exist_ok=True)
        default.write_text(f"#!/bin/sh\necho {name} > ../marker-default-{name}\n", newline="\n")
    for key, value in HOSTILE_CONFIG:
        git(folder, "config", "--local", key, value)
    (folder / ".gitattributes").write_text("*.tex filter=evil diff=evil merge=evil\n", newline="\n")
    runner = gitsync.Git(folder, linked.env.home)
    runner.run("status", "--porcelain")
    runner.run("diff", "HEAD")
    runner.run("add", "--all", "--", ".")
    runner.run(*gitsync._identity(runner), "commit", "-qm", "attributes")
    linked.remote.edit("main.tex", "settling time", "settle", "Remote change")
    runner.run("fetch", "origin")
    runner.run(*gitsync._identity(runner), "merge", "--no-edit", "origin/master", check=False)
    runner.run("checkout", "-q", "HEAD~1")
    runner.run("checkout", "-q", "master")
    runner.run("push", "origin", "HEAD:refs/heads/side", check=False)
    markers = sorted(p.name for p in linked.env.research.glob("marker-*"))
    assert markers == []
    # The local credential helper that would print a token never ran.
    for path in linked.env.store_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()
    # Control: the same repository driven by plain git does run the hostile hook, so the
    # empty marker list above is the overrides' doing, not a broken fixture.
    git(folder, "commit", "--allow-empty", "-qm", "control")
    assert (linked.env.research / "marker-hook-pre-commit").exists()


def test_a_tampered_remote_url_is_refused_before_contacting_it(linked: Linked) -> None:
    git(linked.folder, "remote", "set-url", "origin", f"https://x:{TOKEN}@example.invalid/p")
    with pytest.raises(PrometheosError) as refused:
        linked.preview()
    assert refused.value.code == "policy_denied"
    status = linked.svc.overleaf_status(linked.pid, OWNER, linked.paper_id)
    assert status["remote"]["url"] is None and status["remote"]["url_accepted"] is False
    assert TOKEN not in str(status)


def test_a_slow_or_stalled_git_command_is_logged_by_its_verb_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review cp10-browser R4-F2: the hub's log names a git command that took long (or never
    finished) by its subcommand, duration and exit code, never by its arguments."""
    import logging
    import sys

    monkeypatch.setattr(gitsync, "SLOW_GIT", 0.0)  # every command counts as slow here
    # The git version probe is cached per process; whether it ran already depended on test
    # order under xdist (int9 attempt 1, decision 0057). Clear it so it is always logged too.
    gitsync._git_version.cache_clear()
    runner = gitsync.Git(tmp_path, tmp_path / "home")
    with caplog.at_level(logging.WARNING, logger="prometheos.app.gitsync"):
        runner.run("init", "-q")
        runner.run("-c", "user.name=Private Person", "status", "--short", "--", "secret-path.tex")
    messages = [record.getMessage() for record in caplog.records]
    assert [m.split(" took ")[0] for m in messages] == ["git --version", "git init", "git status"]
    assert all(m.endswith("(exit 0)") for m in messages)
    assert "Private Person" not in caplog.text and "secret-path" not in caplog.text
    caplog.clear()
    monkeypatch.setattr(gitsync, "SLOW_GIT", 2.0)
    with caplog.at_level(logging.WARNING, logger="prometheos.app.gitsync"):
        runner.run("status")  # fast: nothing logged
        stall = [sys.executable, "-c", "import time; time.sleep(10)"]
        with pytest.raises(subprocess.TimeoutExpired):
            gitsync._timed(stall, "fetch", 0.5, capture_output=True, check=False)
    assert [record.getMessage() for record in caplog.records] == [
        "git fetch did not finish within 0.5 s"
    ]
