"""CP11 S11.4b: CP11 so far, usable by the owner (decisions 0066, 0067).

Behaviour tests for the additions of 0066 §2 that the hub journey (e2e 24) drives in a browser:

- research folders: listed; added only by the owner (an agent's request is approval_required,
  the grant then resumes it); the location rules (an existing local folder, not the PrometheOS
  home, a system folder or a folder overlapping another root, a new alias); stored as an
  audited project_update revision; re-checked when an approved request runs; never removed;
- the hub's rule-extraction route answers an operation run that completes with the counts;
- a span cited from a search hit or a reader selection grounds like any proposal (the misquote
  is refused verbatim).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import synthetic_bib as bibs
import synthetic_pdf as pdfs
from cp1_support import AGENT, OWNER, OWNER_ACT, Env

from prometheos.domain.models import PrometheosError


class Flow:
    """A project whose only research root is ``research``, and a papers folder beside it."""

    def __init__(self, tmp: Path) -> None:
        self.env = Env(tmp)
        self.svc, self.pid = self.env.svc, self.env.project_id
        self.papers = tmp / "papers"
        pdfs.write_folder(self.papers, {"drag.pdf": pdfs.paper(pdfs.DRAG)})
        bibs.write_folder(self.papers, bibs.synthetic_set())

    def roots(self) -> dict[str, str]:
        listed = self.svc.project_roots(self.pid, OWNER)
        return {r["alias"]: r["path"] for r in listed["roots"]}

    def rev(self) -> int:
        return int(self.svc.project_info(self.pid, OWNER)["settings_rev"])


@pytest.fixture
def flow(tmp_path: Path) -> Flow:
    return Flow(tmp_path)


def refused(fn: Any, code: str = "validation_error") -> PrometheosError:
    with pytest.raises(PrometheosError) as caught:
        fn()
    assert caught.value.code == code, (caught.value.code, caught.value.message)
    return caught.value


def test_the_owner_adds_a_research_folder_as_an_audited_settings_update(flow: Flow) -> None:
    listed = flow.svc.project_roots(flow.pid, AGENT)
    assert [r["alias"] for r in listed["roots"]] == ["research"]
    assert "cannot be removed" in listed["removal"]
    before = flow.rev()
    done = flow.svc.project_root_add(flow.pid, "papers", str(flow.papers), authority=OWNER_ACT)
    assert done["alias"] == "papers" and done["path"] == str(flow.papers.resolve())
    assert flow.roots()["papers"] == str(flow.papers.resolve())
    assert flow.rev() == before + 1
    with closing(sqlite3.connect(flow.env.store_dir / "record.sqlite")) as conn:
        ops = [
            r[0]
            for r in conn.execute("SELECT operation FROM audit WHERE record_id = ?", (flow.pid,))
        ]
    assert ops[-2:] == ["project_update", "project_root_add:papers"]
    # The folder is now importable.
    plan = flow.svc.library_import_plan(flow.pid, OWNER, root_alias="papers")
    assert any(i["path"] == "drag.pdf" for i in plan["items"])
    assert flow.svc.project_roots(flow.pid, OWNER)["roots"][1]["available"] is True


def test_adding_a_folder_is_the_owners_decision(flow: Flow) -> None:
    before = flow.rev()
    error = refused(
        lambda: flow.svc.approval_request(
            flow.pid, AGENT, "project_root_add", {"alias": "papers", "path": str(flow.papers)}
        ),
        "approval_required",
    )
    assert flow.rev() == before and "papers" not in flow.roots()
    refused(
        lambda: flow.svc.project_root_add(flow.pid, "papers", str(flow.papers), authority=None),
        "policy_denied",
    )
    approval = error.details["approval_request_id"]
    shown = flow.svc.approval_owner_view(flow.pid, approval)
    assert any("add the research folder papers" in line for line in shown["summary"])
    flow.svc.approval_decide(
        flow.pid, approval, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    resumed = flow.svc.approval_resume(flow.pid, AGENT, approval)
    assert resumed["state"] == "used"
    assert flow.roots()["papers"] == str(flow.papers.resolve())


def test_an_approved_folder_is_checked_again_when_it_runs(flow: Flow, tmp_path: Path) -> None:
    gone = tmp_path / "soon-gone"
    gone.mkdir()
    error = refused(
        lambda: flow.svc.approval_request(
            flow.pid, AGENT, "project_root_add", {"alias": "gone", "path": str(gone)}
        ),
        "approval_required",
    )
    approval = error.details["approval_request_id"]
    shown = flow.svc.approval_owner_view(flow.pid, approval)
    flow.svc.approval_decide(
        flow.pid, approval, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    gone.rmdir()
    refused(lambda: flow.svc.approval_resume(flow.pid, AGENT, approval))
    assert "gone" not in flow.roots()


def test_the_location_rules(flow: Flow, tmp_path: Path) -> None:
    def add(alias: str, path: str) -> PrometheosError:
        return refused(
            lambda: flow.svc.project_root_add(flow.pid, alias, path, authority=OWNER_ACT)
        )

    research = flow.env.research
    (research / "inside").mkdir()
    (flow.env.home / "papers-in-home").mkdir(parents=True)
    (tmp_path / "shelf" / "inner").mkdir(parents=True)
    flow.svc.project_root_add(
        flow.pid, "inner", str(tmp_path / "shelf" / "inner"), authority=OWNER_ACT
    )
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    cases = [
        ("missing", str(tmp_path / "does-not-exist"), "does not exist"),
        ("file", str(a_file), "does not exist"),
        ("relative", "papers", "absolute path"),
        ("network", "\\\\server\\share\\papers", "network"),
        ("inside", str(research / "inside"), "overlaps the research folder research"),
        ("outer", str(tmp_path), "PrometheOS home"),  # it holds the home too
        ("container", str(tmp_path / "shelf"), "overlaps the research folder inner"),
        ("home", str(flow.env.home / "papers-in-home"), "PrometheOS home"),
        ("store", str(flow.env.store_dir), "PrometheOS home"),
        ("drive", str(Path(tmp_path.anchor)), "root cannot be a research folder"),
        ("profile", str(Path.home()), "whole home or users folder"),
        ("research", str(flow.papers), "already a research folder"),
        ("Bad Alias", str(flow.papers), "an alias is"),
    ]
    if sys.platform == "win32" and os.environ.get("SystemRoot"):
        cases.append(("system", os.environ["SystemRoot"], "system folder"))
    for alias, path, message in cases:
        error = add(alias, path)
        assert message in error.message, (alias, error.message)
    assert list(flow.roots()) == ["research", "inner"]


def test_a_plan_made_on_older_settings_is_refused(flow: Flow) -> None:
    from prometheos.app import roots

    plan = flow.svc.project_root_plan(flow.pid, "papers", str(flow.papers))
    other = flow.papers.parent / "second"
    other.mkdir()
    flow.svc.project_root_add(flow.pid, "second", str(other), authority=OWNER_ACT)
    error = refused(
        lambda: roots.execute_add(
            flow.svc.open(flow.pid),
            plan,
            "test",
            home=flow.svc.home,
            request_id="00000000-0000-4000-8000-0000000000c1",
        ),
        "revision_conflict",
    )
    assert "settings changed" in error.message
    assert "papers" not in flow.roots()


def test_cli_lists_folders_and_asks_for_an_addition(flow: Flow, tmp_path: Path) -> None:
    from test_cp1_cli import Cli

    cli = Cli(tmp_path)
    code, listed, _ = cli("project", "root", "list", "--project", flow.pid)
    assert code == 0 and [r["alias"] for r in listed["roots"]] == ["research"]
    code, asked, _ = cli(
        "project", "root", "add", "papers", str(flow.papers), "--project", flow.pid
    )
    assert code == 4 and asked["error"]["code"] == "approval_required"
    assert "papers" not in flow.roots()


def test_the_hub_rule_run_completes_with_counts(flow: Flow) -> None:
    from hub_support import running_hub

    flow.svc.project_root_add(flow.pid, "papers", str(flow.papers), authority=OWNER_ACT)
    plan = flow.svc.library_import_plan(flow.pid, OWNER, root_alias="papers")
    flow.svc.library_import_commit(flow.pid, plan["plan_id"], OWNER)
    drag = next(
        w["work_id"]
        for w in flow.svc.library_works(flow.pid, OWNER, limit=200)["works"]
        if w["title"].startswith("Aerodynamic")
    )
    columns = [{"id": "finding", "label": "Finding", "value_kind": "text", "instructions": ""}]
    matrix = flow.svc.matrix_create(
        flow.pid, OWNER, title="Rules from the hub", columns=columns, work_ids=[drag]
    )["matrix_id"]
    rules = {
        "rules": [{"id": "f", "column": "finding", "pattern": "reduces windage loss by {value} %"}]
    }
    with running_hub(flow.svc, flow.pid) as hub:
        hub.pair()
        reply = hub.request(
            "POST", f"/api/v1/matrix/{matrix}/extract/run", {"rules": rules, "wait_seconds": 20}
        )
        assert reply.status == 200, reply.body
        run = reply.json()
        deadline = time.monotonic() + 60
        while run["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.2)
            run = hub.request("GET", f"/api/v1/runs/{run['run_id']}").json()
        assert run["state"] == "completed", run
        assert run["result"]["counts"] == {"created": 1}
        assert run["operation"] == "matrix_extract"
        bad = hub.request(
            "POST",
            f"/api/v1/matrix/{matrix}/extract/run",
            {"rules": {"rules": [{"id": "x", "column": "nope", "keywords": ["a"]}]}},
        )
        assert bad.status == 400 and "no column nope" in bad.json()["error"]["message"]
    cell = flow.svc.matrix_show(flow.pid, OWNER, matrix)["rows"][0]["cells"][0]
    assert cell["value_text"] == "reduces windage loss by 92 %"


def test_a_cited_span_grounds_like_any_proposal(flow: Flow) -> None:
    flow.svc.project_root_add(flow.pid, "papers", str(flow.papers), authority=OWNER_ACT)
    plan = flow.svc.library_import_plan(flow.pid, OWNER, root_alias="papers")
    flow.svc.library_import_commit(flow.pid, plan["plan_id"], OWNER)
    drag = next(
        w["work_id"]
        for w in flow.svc.library_works(flow.pid, OWNER, limit=200)["works"]
        if w["title"].startswith("Aerodynamic")
    )
    columns = [
        {"id": "drag", "label": "Drag coefficient", "value_kind": "quantity", "instructions": ""}
    ]
    matrix = flow.svc.matrix_create(
        flow.pid, OWNER, title="Cite", columns=columns, work_ids=[drag]
    )["matrix_id"]
    hit = next(
        h
        for h in flow.svc.library_search(flow.pid, OWNER, "drag coefficient", work_id=drag)["hits"]
        if h["page"] == 2
    )
    # A search hit quotes only the words found: the number is not in that span.
    error = refused(
        lambda: flow.svc.matrix_propose(
            flow.pid,
            OWNER,
            matrix_id=matrix,
            work_id=drag,
            column_id="drag",
            value_text="0.0042",
            evidence=[hit["evidence"]],
            quantities=[{"decimal": "0.0042", "unit": ""}],
        )
    )
    assert error.message == (
        "cell drag: 0.0042 does not occur verbatim in the quoted evidence (no tolerance; "
        "numbers below ten are checked too)"
    )
    # A selection in the reader: the hub asks the server for the span's evidence (read).
    sentence = "The drag coefficient was 0.0042 at 10 Pa."
    text = flow.svc.library_read(flow.pid, OWNER, drag, page=2, max_chars=8000)["text"]
    at = text.index(sentence)
    read = flow.svc.library_read(flow.pid, OWNER, drag, page=2, start=at, end=at + len(sentence))
    flow.svc.matrix_propose(
        flow.pid,
        OWNER,
        matrix_id=matrix,
        work_id=drag,
        column_id="drag",
        value_text="0.0042",
        evidence=[read["evidence"]],
        quantities=[{"decimal": "0.0042", "unit": ""}],
    )
    cell = flow.svc.matrix_show(flow.pid, OWNER, matrix)["rows"][0]["cells"][0]
    assert cell["state"] == "proposed" and cell["method"] == "manual"
    assert cell["basis"] == "full_text"
    assert cell["evidence"][0]["where"] == {
        "work_id": drag,
        "page": 2,
        "start": at,
        "end": at + len(sentence),
    }


# ----- S11.4b review round 1 (decision 0067, "Review round 1 repairs") ---------------------------


def _clean_bases(monkeypatch: pytest.MonkeyPatch, user: Path) -> None:
    """The user's home is ``user`` and no OneDrive is configured (deterministic bases)."""
    monkeypatch.setattr(Path, "home", lambda: user)
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(env, raising=False)


def _ask(flow: Flow, alias: str, path: str) -> str:
    error = refused(
        lambda: flow.svc.approval_request(
            flow.pid, AGENT, "project_root_add", {"alias": alias, "path": path}
        ),
        "approval_required",
    )
    return str(error.details["approval_request_id"])


def test_the_owner_sees_the_requested_folder_in_the_hub(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the hub's Approvals reply names the folder an agent asks for, exactly, without an
    absolute path: a base placeholder, the folders below it and warnings."""
    from hub_support import running_hub

    from prometheos.app import roots

    user = tmp_path / "users" / "me"
    wanted = user / "Documents" / "papers" / "2026"
    hidden = user / "Documents" / ".private-keys"
    wanted.mkdir(parents=True)
    hidden.mkdir(parents=True)
    _clean_bases(monkeypatch, user)
    monkeypatch.setattr(roots, "_users_folders", lambda: [tmp_path / "users"])
    papers_id = _ask(flow, "papers", str(wanted))
    keys_id = _ask(flow, "keys", str(hidden))
    with running_hub(flow.svc, flow.pid) as hub:
        hub.pair()
        reply = hub.request("GET", f"/api/v1/approvals/{papers_id}")
        assert reply.status == 200, reply.body
        shown = reply.json()
        assert shown["summary"] == [
            "add the research folder papers: your Documents > papers > 2026"
        ]
        view = {"base": "your Documents", "segments": ["papers", "2026"], "warnings": []}
        assert shown["plan"]["folder_view"] == view
        assert "path" not in shown["plan"]
        assert shown["preview"] == {
            "kind": "project_root_add",
            "folder_view": view,
            "matches_plan": True,
            "root_changes": [],  # the hub's restored owner field (decision 0023 item 8), empty
        }
        keys = hub.request("GET", f"/api/v1/approvals/{keys_id}").json()
        assert keys["summary"] == [
            "add the research folder keys: your Documents > .private-keys",
            "warning: hidden folder: .private-keys",
            "warning: sensitive name: .private-keys",
        ]
        queue = hub.request("GET", "/api/v1/approvals").json()
        assert str(tmp_path) not in json.dumps(queue) + json.dumps(shown) + json.dumps(keys)
        assert str(tmp_path).replace("\\", "/") not in json.dumps(shown)
        granted = hub.request(
            "POST",
            f"/api/v1/approvals/{papers_id}/grant",
            {"plan_sha256": shown["plan_sha256"]},
        )
        assert granted.status == 200, granted.body
    assert flow.svc.approval_resume(flow.pid, AGENT, papers_id)["state"] == "used"
    assert flow.roots()["papers"] == str(wanted.resolve())


def test_an_agents_request_reveals_nothing_about_the_computers_folders(
    flow: Flow, tmp_path: Path
) -> None:
    """F5: before the owner decides, a request is checked without touching the disk. Every
    location rule gives the same refusal; a folder that does not exist is simply asked for, and
    only the owner's review (and the grant) say it does not exist."""
    from prometheos.app import roots

    keys = tmp_path / ".ssh"
    keys.mkdir()
    refusals = [
        "papers",  # relative
        "\\\\server\\share\\papers",
        str(Path(tmp_path.anchor)),
        str(flow.env.home / "inside-home"),
        str(flow.env.research / "inside"),
        str(keys),
        str(keys / "no-such-folder"),  # the same answer whether or not it exists
        str(Path.home()),
    ]
    if sys.platform == "win32" and os.environ.get("SystemRoot"):
        refusals.append(os.environ["SystemRoot"] + "\\no-such-folder")
    for path in refusals:
        error = refused(
            lambda path=path: flow.svc.approval_request(
                flow.pid, AGENT, "project_root_add", {"alias": "probe", "path": path}
            )
        )
        assert (error.message, error.details) == (roots.REQUEST_REFUSED, {}), path
    # The alias rules are named: they say nothing about this computer.
    error = refused(
        lambda: flow.svc.approval_request(
            flow.pid, AGENT, "project_root_add", {"alias": "research", "path": str(tmp_path / "x")}
        )
    )
    assert error.message == "this alias is already a research folder"
    # A folder that does not exist is asked for like any other ...
    missing = _ask(flow, "missing", str(tmp_path / "no-such-folder"))
    shown = flow.svc.approval_owner_view(flow.pid, missing)
    # ... and only the owner learns why it cannot be added.
    assert shown["preview"]["matches_plan"] is False
    assert shown["preview"]["error"] == (
        "cannot be added: the folder does not exist (or is not a directory)"
    )
    # The grant itself is refused while the review does not match (the hub disables Grant).
    refused(
        lambda: flow.svc.approval_decide(
            flow.pid, missing, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
        ),
        "revision_conflict",
    )
    assert "missing" not in flow.roots()


def test_key_folders_appdata_and_other_profiles_are_refused(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: folders of keys, AppData (the temporary folder inside it excepted), another user's
    profile and the shared Public profile are refused by default, with the reason."""
    from prometheos.app import roots

    users = tmp_path / "users"
    me, other, public = users / "me", users / "other" / "papers", users / "Public" / "papers"
    for folder in (me / "Documents" / "papers", other, public, me / "AppData" / "Roaming" / "x"):
        folder.mkdir(parents=True)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / "keys" / ".gnupg").mkdir(parents=True)
    _clean_bases(monkeypatch, me)
    monkeypatch.setattr(roots, "_users_folders", lambda: [users])

    def add(alias: str, path: Path) -> str:
        return refused(
            lambda: flow.svc.project_root_add(flow.pid, alias, str(path), authority=OWNER_ACT)
        ).message

    assert (
        add("ssh", tmp_path / ".ssh")
        == "a folder of keys (.ssh, .gnupg) cannot be a research folder"
    )
    assert "folder of keys" in add("gpg", tmp_path / "keys" / ".gnupg")
    with monkeypatch.context() as no_temp:  # this test's folders are in the temporary folder
        no_temp.setattr(roots, "_temporary", lambda: [])
        assert add("appdata", me / "AppData" / "Roaming" / "x") == (
            "AppData holds programs' data and saved credentials; it cannot be a research folder"
        )
    assert add("other", other) == "another user's profile folder cannot be a research folder"
    assert add("public", public) == "the shared Public profile cannot be a research folder"
    # The owner's own folders are fine, and so is the temporary folder (inside AppData on
    # Windows), where downloads are unpacked and this test's folders live.
    flow.svc.project_root_add(
        flow.pid, "mine", str(me / "Documents" / "papers"), authority=OWNER_ACT
    )
    flow.svc.project_root_add(flow.pid, "papers", str(flow.papers), authority=OWNER_ACT)
    assert sorted(flow.roots()) == ["mine", "papers", "research"]
    if sys.platform == "win32":
        monkeypatch.undo()
        real = roots._users_folders()[0]
        if (real / "Public").is_dir():
            assert "Public profile" in add("realpublic", real / "Public")
        roaming = Path.home() / "AppData" / "Roaming"
        if roaming.is_dir():
            assert "AppData" in add("roaming", roaming)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows profiles directory")
def test_the_users_folder_comes_from_windows_not_from_userprofile(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration run int10: the launcher sets USERPROFILE to a synthetic folder beside a
    synthetic TEMP. Taking USERPROFILE's parent as the users folder made every folder in that
    TEMP "another user's profile". The users folder is Windows' own profiles directory."""
    from prometheos.app import roots

    real = roots._users_folders()
    assert real and real[0].is_dir()
    sandbox = tmp_path / "pl1"
    profile, temp = sandbox / "h", sandbox / "t"
    papers = temp / "papers"
    papers.mkdir(parents=True)
    profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setattr(roots, "_temporary", lambda: [temp, temp.resolve()])
    assert roots._users_folders() == real  # unchanged by USERPROFILE
    flow.svc.project_root_add(flow.pid, "sandboxed", str(papers), authority=OWNER_ACT)
    assert "sandboxed" in flow.roots()


def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_links_are_followed_and_a_requested_link_is_refused(flow: Flow, tmp_path: Path) -> None:
    """F6: a junction (Windows) or symbolic link is resolved before the rules: one to a system
    folder is refused; one to an ordinary folder is stored as its target when the owner adds
    it; an agent's request for a link is refused when it runs (the owner approves the folder
    they were shown, not another one behind it)."""
    from prometheos.app import roots

    system = Path(os.environ["SystemRoot"]) if sys.platform == "win32" else Path("/etc")
    _link(tmp_path / "to-system", system)
    error = refused(
        lambda: flow.svc.project_root_add(
            flow.pid, "sys", str(tmp_path / "to-system"), authority=OWNER_ACT
        )
    )
    assert error.message == "a system folder cannot be a research folder"
    real = tmp_path / "real-papers"
    real.mkdir()
    _link(tmp_path / "to-real", real)
    done = flow.svc.project_root_add(
        flow.pid, "linked", str(tmp_path / "to-real"), authority=OWNER_ACT
    )
    assert done["path"] == str(real.resolve()) == flow.roots()["linked"]
    other = tmp_path / "other-real"
    other.mkdir()
    _link(tmp_path / "to-other", other)
    asked = _ask(flow, "asked", str(tmp_path / "to-other"))
    shown = flow.svc.approval_owner_view(flow.pid, asked)
    assert shown["preview"]["matches_plan"] is False
    assert shown["preview"]["error"] == roots.LINKED
    refused(
        lambda: flow.svc.approval_decide(
            flow.pid, asked, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
        ),
        "revision_conflict",
    )
    # A folder replaced by a link after the grant is refused when the grant runs.
    swapped = tmp_path / "swapped"
    swapped.mkdir()
    later = _ask(flow, "later", str(swapped))
    shown = flow.svc.approval_owner_view(flow.pid, later)
    assert shown["preview"]["matches_plan"] is True
    flow.svc.approval_decide(
        flow.pid, later, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    swapped.rmdir()
    _link(swapped, other)
    assert refused(lambda: flow.svc.approval_resume(flow.pid, AGENT, later)).message == roots.LINKED
    assert "asked" not in flow.roots() and "later" not in flow.roots()


def test_a_sync_folder_is_accepted(
    flow: Flow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0067 §1: a OneDrive (or other sync) folder is only read, so it may be a research folder;
    the owner's view names it as OneDrive."""
    onedrive = tmp_path / "OneDrive - Synthetic University"
    papers = onedrive / "papers"
    papers.mkdir(parents=True)
    _clean_bases(monkeypatch, tmp_path / "users" / "me")
    monkeypatch.setenv("OneDrive", str(onedrive))
    asked = _ask(flow, "cloud", str(papers))
    shown = flow.svc.approval_owner_view(flow.pid, asked)
    assert shown["summary"] == ["add the research folder cloud: your OneDrive > papers"]
    assert shown["preview"]["matches_plan"] is True
    flow.svc.approval_decide(
        flow.pid, asked, grant=True, plan_sha256=shown["plan_sha256"], owner=OWNER_ACT
    )
    flow.svc.approval_resume(flow.pid, AGENT, asked)
    assert flow.roots()["cloud"] == str(papers.resolve())


def test_describe_folder_bases_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prometheos.app import roots

    me = tmp_path / "users" / "me"
    _clean_bases(monkeypatch, me)
    assert roots.describe_folder(str(me / "Desktop" / "Lit")) == {
        "base": "your Desktop", "segments": ["Lit"], "warnings": [],
    }  # fmt: skip
    assert roots.describe_folder(str(me / "work" / "credentials")) == {
        "base": "your user folder",
        "segments": ["work", "credentials"],
        "warnings": ["sensitive name: credentials"],
    }
    if sys.platform == "win32":
        assert roots.describe_folder("Z:\\data\\papers")["base"] == "drive Z"
    else:
        assert roots.describe_folder("/srv/papers")["base"] == "the file-system root"
