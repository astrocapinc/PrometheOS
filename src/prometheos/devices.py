"""Remembered devices (A07, CP21.2; decision 0089).

A successful pairing on the home's own host name, ``h<16 hex>.localhost``, may leave a remembered
device credential in that browser: a host-only cookie with a per-home name holding
``<device id>.<secret>``. The home keeps only the secret's digest, in
``<home>/launcher/devices.sqlite``. After a core restart the credential is exchanged for a new
short-lived session without a new pairing.

- **Rotation.** Each exchange replaces the secret; the previous one stays valid for 60 seconds
  so that several tabs exchanging at once all keep their session.
- **Expiry.** 14 days without use, and 90 days after creation at most.
- **Revocation.** The owner revokes from the hub or the CLI; it ends the current and the previous
  secret at once, and the hub ends that device's live sessions.

The credential never grants more than an owner session, and an agent token never uses it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prometheos.domain.models import PrometheosError

IDLE_SECONDS = 14 * 86400.0
MAX_SECONDS = 90 * 86400.0
GRACE_SECONDS = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    previous_sha256 TEXT,
    previous_until REAL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
)
"""


def host_label(home_id: str) -> str:
    """The home's DNS label: ``h`` and the first 16 hexadecimal characters of its UUID."""
    return "h" + uuid.UUID(home_id).hex[:16]


def cookie_name(label: str) -> str:
    return f"prometheos_device_{label}"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def _db(home: Path) -> Iterator[sqlite3.Connection]:
    path = home / "launcher" / "devices.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        yield conn
    finally:
        conn.close()


def browser_label(user_agent: str | None) -> str:
    agent = (user_agent or "").lower()
    for marker, name in (("edg/", "Edge"), ("firefox/", "Firefox"), ("chrome/", "Chrome")):
        if marker in agent:
            return f"{name} on {'Windows' if 'windows' in agent else 'this computer'}"
    return "A browser"


def remember(home: Path, label: str, now: float | None = None) -> str:
    """A new remembered device; returns the cookie value (shown to nobody but the browser)."""
    moment = time.time() if now is None else now
    device_id = "dev_" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    with _db(home) as conn:
        conn.execute(
            "INSERT INTO devices (device_id, label, secret_sha256, created_at, last_used_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, label[:80], _digest(secret), moment, moment, moment + MAX_SECONDS),
        )
    return f"{device_id}.{secret}"


def resume(
    home: Path, value: str | None, now: float | None = None
) -> tuple[str, str | None] | None:
    """Exchange a presented credential: ``(device id, new cookie value or None)``, or None when
    it is unknown, revoked or expired. The current secret rotates; the previous one, inside its
    60-second grace, is accepted without a further rotation."""
    if not value or "." not in value or not value.isascii():
        return None  # a non-ASCII value is simply unknown, never a server error (A6)
    device_id, _, secret = value.partition(".")
    if not device_id.startswith("dev_") or not secret:
        return None
    moment = time.time() if now is None else now
    presented = _digest(secret)
    with _db(home) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT secret_sha256, previous_sha256, previous_until, last_used_at, expires_at, "
                "revoked_at FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            current, previous, previous_until, last_used, expires, revoked = row
            if revoked is not None or moment > expires or moment > last_used + IDLE_SECONDS:
                conn.execute("ROLLBACK")
                return None
            if hmac.compare_digest(presented, current):
                fresh = secrets.token_urlsafe(32)
                conn.execute(
                    "UPDATE devices SET secret_sha256 = ?, previous_sha256 = ?, "
                    "previous_until = ?, last_used_at = ? WHERE device_id = ?",
                    (_digest(fresh), current, moment + GRACE_SECONDS, moment, device_id),
                )
                conn.execute("COMMIT")
                return device_id, f"{device_id}.{fresh}"
            if (
                previous is not None
                and previous_until is not None
                and moment <= previous_until
                and hmac.compare_digest(presented, previous)
            ):
                conn.execute(
                    "UPDATE devices SET last_used_at = ? WHERE device_id = ?", (moment, device_id)
                )
                conn.execute("COMMIT")
                return device_id, None
            conn.execute("ROLLBACK")
            return None
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def is_revoked(home: Path, device_id: str) -> bool:
    with _db(home) as conn:
        row = conn.execute(
            "SELECT revoked_at FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
    return row is None or row[0] is not None


def list_devices(home: Path, now: float | None = None) -> list[dict[str, Any]]:
    moment = time.time() if now is None else now
    with _db(home) as conn:
        rows = conn.execute(
            "SELECT device_id, label, created_at, last_used_at, expires_at, revoked_at "
            "FROM devices ORDER BY created_at"
        ).fetchall()
    out = []
    for device_id, label, created, last_used, expires, revoked in rows:
        idle_until = last_used + IDLE_SECONDS
        state = "revoked" if revoked is not None else "active"
        if state == "active" and moment > min(expires, idle_until):
            state = "expired"
        out.append(
            {
                "device_id": device_id,
                "label": label,
                "state": state,
                "created_at": _iso(created),
                "last_used_at": _iso(last_used),
                "expires_at": _iso(min(expires, idle_until)),
                "revoked_at": _iso(revoked),
            }
        )
    return out


def revoke(home: Path, device_id: str, now: float | None = None) -> dict[str, Any]:
    """Revoke a device: its current and its previous secret stop working at once."""
    moment = time.time() if now is None else now
    with _db(home) as conn:
        changed = conn.execute(
            "UPDATE devices SET revoked_at = COALESCE(revoked_at, ?), previous_sha256 = NULL, "
            "previous_until = NULL WHERE device_id = ?",
            (moment, device_id),
        ).rowcount
    if not changed:
        raise PrometheosError("not_found", "no remembered device with that id", {})
    return {"device_id": device_id, "revoked": True}
