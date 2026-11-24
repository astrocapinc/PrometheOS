"""Capability operations added by CP9 (overleaf_push) and CP6 (provider_*) after the merge: the
set of operations that must be bound to an exact digest is pinned, and each one is refused
without a digest. Dropping the requirement for one branch's operation in a careless merge fails
here."""

from __future__ import annotations

import sqlite3

import pytest
from cp1_support import Env

from prometheos.domain.models import PrometheosError
from prometheos.store import capabilities, db

DIGEST_BOUND = {
    "exact_request",
    "purge",
    "overleaf_push",  # CP9
    "provider_configure",  # CP6.3
    "provider_run",
    "provider_egress",
}


def test_the_digest_required_set_is_exactly_the_documented_one() -> None:
    assert capabilities.DIGEST_REQUIRED == DIGEST_BOUND
    assert DIGEST_BOUND <= capabilities.OPERATIONS
    assert {"read_sensitive", "full_data_export"} <= capabilities.OPERATIONS
    assert not (capabilities.DIGEST_REQUIRED & {"read_sensitive", "full_data_export"})


@pytest.mark.parametrize("operation", sorted(DIGEST_BOUND))
def test_every_digest_bound_operation_refuses_a_capability_without_a_digest(
    tmp_path, operation: str
) -> None:
    env = Env(tmp_path)
    conn = env.svc.open(env.project_id).connect()
    try:
        with db.write_transaction(conn):
            with pytest.raises(PrometheosError) as err:
                capabilities.issue(
                    conn, project_id=env.project_id, operation=operation, digest=None,
                    ttl_seconds=60, max_uses=1, label="SYNTHETIC-TEST",
                )  # fmt: skip
            assert err.value.code == "validation_error" and "need a digest" in err.value.message
            grant = capabilities.issue(
                conn, project_id=env.project_id, operation=operation, digest="a" * 64,
                ttl_seconds=60, max_uses=1, label="SYNTHETIC-TEST",
            )  # fmt: skip
            assert grant.digest == "a" * 64
            with pytest.raises(PrometheosError):  # and a different digest never matches
                capabilities.consume(
                    conn, secret=grant.secret, project_id=env.project_id, operation=operation,
                    digest="b" * 64,
                )  # fmt: skip
    finally:
        conn.close()
    assert isinstance(conn, sqlite3.Connection)


def test_unknown_operations_are_refused(tmp_path) -> None:
    env = Env(tmp_path)
    conn = env.svc.open(env.project_id).connect()
    try:
        with db.write_transaction(conn), pytest.raises(PrometheosError):
            capabilities.issue(
                conn, project_id=env.project_id, operation="provider_send_anything",
                digest="a" * 64, ttl_seconds=60, max_uses=1, label="SYNTHETIC-TEST",
            )  # fmt: skip
    finally:
        conn.close()
