"""Review 0009 R2-L4: a replay must not reveal restricted records across caller scopes.

A request committed at owner scope and resent with the same request ID and body at agent
scope used to replay the stored response, including IDs of the restricted records it
created. The stored request key now binds the caller's visibility rank.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cp1_support import AGENT, OWNER, Env, counts, create_entry, request

from prometheos.app.apply import _parse_with_value
from prometheos.domain.canonical import sha256_hex
from prometheos.domain.ids import new_id, new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.store.project import rank_bound_sha256


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_owner_request_resent_at_agent_scope_conflicts_without_leaking(env: Env) -> None:
    secret_id = new_id("entry")
    raw = request(
        env.project_id, [create_entry(secret_id, sensitivity="restricted", title="Embargo")], 1
    )
    first = env.svc.apply(env.project_id, raw, OWNER)
    assert first["replayed"] is False
    before = counts(env.store_dir)
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, raw, AGENT)
    assert err.value.code == "request_key_conflict"
    assert secret_id not in repr(err.value.details) and secret_id not in str(err.value)
    assert counts(env.store_dir) == before


def test_same_scope_retries_still_replay(env: Env) -> None:
    for caller in (OWNER, AGENT):
        raw = request(env.project_id, [create_entry(title="Retry")], env.seq())
        first = env.svc.apply(env.project_id, raw, caller)
        second = env.svc.apply(env.project_id, raw, caller)
        assert second["replayed"] is True
        assert {**second, "replayed": False} == first


def test_agent_request_resent_at_owner_scope_conflicts(env: Env) -> None:
    raw = request(env.project_id, [create_entry(title="Agent note")], 1)
    env.svc.apply(env.project_id, raw, AGENT)
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, raw, OWNER)
    assert err.value.code == "request_key_conflict"


def test_pre_fix_rows_replay_only_at_owner_rank(env: Env) -> None:
    rid = new_request_id()
    raw = request(env.project_id, [create_entry(title="Legacy")], 1, rid)
    first = env.svc.apply(env.project_id, raw, OWNER)
    # Rewrite the stored key to its pre-fix form: the bare request digest.
    bare = _parse_with_value(raw)[1]
    conn = sqlite3.connect(env.store_dir / "record.sqlite")
    try:
        conn.execute("UPDATE requests SET request_sha256 = ? WHERE request_id = ?", (bare, rid))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(PrometheosError) as err:
        env.svc.apply(env.project_id, raw, AGENT)
    assert err.value.code == "request_key_conflict"
    replay = env.svc.apply(env.project_id, raw, OWNER)
    assert replay["replayed"] is True and {**replay, "replayed": False} == first


def test_rank_binding_changes_the_stored_key() -> None:
    digest = sha256_hex(b"request")
    assert rank_bound_sha256(digest, 0) != rank_bound_sha256(digest, 2)
    assert rank_bound_sha256(digest, 2) != digest
