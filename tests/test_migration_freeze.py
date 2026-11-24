"""Migrations 15 and 16 are frozen for the release (release/papers-1, 2026-11-01).

Once a real home runs a migration its SQL can never change: a store that ran the old text and a
store that ran the new one would disagree with no way to tell. Before this release no real home
had run 15 or 16 (the owner's store is at 14), so they could still change. From the release on,
any change to the schema is a new migration (17 and later); these pins make an edit to 15 or 16
fail loudly instead.
"""

from __future__ import annotations

import hashlib

from prometheos.store import schema

FROZEN = {
    15: (
        "adoption family class decisions (A06.1, CP20.4)",
        "V15_CLASS_DECISIONS",
        "acb26a550be005c716fb96bdbbc057a41de1c56ca3d9983b106b658a7035ebb2",
    ),
    16: (
        "the intelligence layer: policy, results, root sensitivity (A07, CP22); adoption "
        "outlines (A06.1, CP20.4)",
        "V16_INTELLIGENCE",
        "2642a207234024001626d7517f9515b8523a0456753d8769b215f27b0ed606d2",
    ),
}


def test_frozen_migrations_never_change() -> None:
    for version, (name, constant, digest) in FROZEN.items():
        migration = schema.MIGRATIONS[version - 1]
        assert (migration.version, migration.name) == (version, name)
        sql = getattr(schema, constant)
        assert hashlib.sha256(sql.encode("utf-8")).hexdigest() == digest, (
            f"migration {version} is frozen: put the change in a new migration instead"
        )


def test_migrations_stay_numbered_in_order() -> None:
    assert [m.version for m in schema.MIGRATIONS] == list(range(1, len(schema.MIGRATIONS) + 1))


def test_the_hub_port_range_avoids_both_ephemeral_ranges() -> None:
    """Decision 0114: with WSL in mirrored networking mode, Windows and Linux share one port
    space; the hub's fixed ports must sit below Linux's ephemeral range (32768-60999) and
    Windows' dynamic range (49152-65535)."""
    from prometheos import launcher

    assert launcher.PORT_BASE >= 1024
    assert launcher.PORT_BASE + launcher.PORT_SPAN + 50 <= 32768
