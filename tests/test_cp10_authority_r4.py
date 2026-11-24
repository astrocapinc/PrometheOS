"""CP10.2 round 4 (review cp10-2-a-r3 lows; decision 0016 "Round 4").

R3-1: the approval binding key is exactly 32 bytes and appears atomically; an empty or
truncated key file is refused, never used. R3-2: on Windows the ACL of a private file grants
the current user only, with no inherited entries. R3-3: each layer of the recording-start
owner check is pinned on its own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env, create_entry
from test_cp10_authority import grant, refused, rows

from prometheos.app import approvals
from prometheos.domain.models import PrometheosError


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ----- R3-1 ------------------------------------------------------------------------------------


def test_a_fresh_key_is_complete_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    key = approvals.binding_key(home)
    assert len(key) == approvals.BINDING_KEY_BYTES
    folder = (home / approvals.BINDING_KEY_FILE).parent
    assert [p.name for p in folder.iterdir()] == [approvals.BINDING_KEY_FILE.name]
    assert approvals.binding_key(home) == key  # created once, then reused


def test_an_existing_key_is_never_overwritten(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / approvals.BINDING_KEY_FILE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"k" * 32)
    assert approvals.binding_key(home) == b"k" * 32
    assert path.read_bytes() == b"k" * 32


@pytest.mark.parametrize("damaged", [b"", b"x" * 16, b"y" * 33])
def test_a_damaged_key_file_is_refused_not_used(env: Env, damaged: bytes) -> None:
    path = env.home / approvals.BINDING_KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(damaged)
    with pytest.raises(PrometheosError) as err:
        approvals.binding_key(env.home)
    assert err.value.code == "io_error" and f"{len(damaged)} bytes" in err.value.message
    # A request that would bind an owner view fails closed: nothing is stored.
    hidden = create_entry(sensitivity="restricted", title="Hidden")
    env.apply([hidden], caller=OWNER)
    plan = env.svc.purge_plan(env.project_id, [hidden["id"]], OWNER)
    out = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "retention_purge_commit", {"plan_id": plan["plan_id"]}
        )
    )
    assert out.code == "io_error"
    assert not rows(env, "SELECT id FROM approval_requests")
    assert path.read_bytes() == damaged  # left for the owner to inspect, never replaced


def test_a_creator_that_dies_before_the_link_leaves_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"

    def killed(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise RuntimeError("process killed between write and link")

    monkeypatch.setattr(os, "link", killed)
    with pytest.raises(RuntimeError):
        approvals.binding_key(home)
    folder = (home / approvals.BINDING_KEY_FILE).parent
    assert list(folder.iterdir()) == []  # no partial key under the real name, no temp file
    monkeypatch.undo()
    assert len(approvals.binding_key(home)) == approvals.BINDING_KEY_BYTES


def test_a_lost_creation_race_uses_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    path = home / approvals.BINDING_KEY_FILE
    real_link = os.link

    def racing(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        Path(dst).write_bytes(b"w" * 32)  # another process linked its key first
        real_link(src, dst)  # raises FileExistsError: never overwrites

    monkeypatch.setattr(os, "link", racing)
    assert approvals.binding_key(home) == b"w" * 32
    assert path.read_bytes() == b"w" * 32
    assert [p.name for p in path.parent.iterdir()] == [path.name]


# ----- R3-2: the Windows ACL -------------------------------------------------------------------


def _acl_entries(path: Path) -> list[str]:
    done = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, timeout=30, check=True
    )
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    entries = []
    for line in lines:
        if line.lower().startswith("successfully processed"):
            continue
        if line.startswith(str(path)):
            line = line[len(str(path)) :].strip()
        entries.append(line)
    return entries


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs (POSIX modes are checked elsewhere)")
def test_private_files_grant_the_current_user_only_on_windows(tmp_path: Path) -> None:
    from prometheos.cli import _write_agent_token
    from prometheos.hub.security import Gate
    from prometheos.store.private_files import write_private

    user = os.environ["USERNAME"].lower()
    secret = tmp_path / "secret.bin"
    write_private(secret, b"abc")
    token = tmp_path / "agent.token"
    _write_agent_token(token, Gate(port=1))
    home = tmp_path / "home"
    approvals.binding_key(home)
    for path in (secret, token, home / approvals.BINDING_KEY_FILE):
        entries = _acl_entries(path)
        assert len(entries) == 1, (path.name, entries)
        assert user in entries[0].lower() and "(F)" in entries[0], entries
        assert "(I)" not in entries[0]  # nothing inherited from the folder


# ----- R3-3: each owner-check layer of a recording start -----------------------------------------


def test_the_service_owner_check_refuses_before_the_executor(env: Env) -> None:
    params = {"title": "Lab", "device_ids": ["synthetic-mic"]}
    err = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "recording_start", params)
    )
    approval_id = err.details["approval_request_id"]
    grant(env, approval_id)
    out = refused(
        lambda: env.svc.approval_start_recording(
            env.project_id, approval_id, owner=None, plan_sha256=err.details["plan_sha256"],
            start_capture=False,
        )
    )  # fmt: skip
    # require_owner's own refusal (the executor's capture-owner check is the second layer).
    assert out.code == "policy_denied"
    assert "starting a granted recording is an owner decision" in out.message
