"""Local decision capabilities (spec/04 "Decision capabilities").

The owner reviews a concrete proposal and the server issues a capability: an
opaque random secret (only its SHA-256 is stored) bound to an exact request
digest or to a narrow operation scope, with a maximum use count, expiry and
policy version. Clients present the secret as transport metadata; it is never
part of a request hash, a log, a context or a record. Use is checked and
counted inside the same write transaction as the protected mutation.

This guards against accidental and untrusted model instructions. It is not a
security boundary against malicious software running as the same OS user.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheos.domain.models import PrometheosError, utc_now

POLICY_VERSION = 1
# Operation names a capability may authorise.
OPERATIONS = frozenset(
    {
        "exact_request",  # any protected effect inside one exactly reviewed request
        "read_sensitive",  # owner-scope reads of restricted/private records
        "full_data_export",  # owner backups
        "purge",  # owner purge commit (exact plan)
        "overleaf_push",  # one Overleaf Git push of an exact previewed plan (A01, CP9)
        "provider_configure",  # CP6.3: provider settings or credential change (exact digest)
        "provider_run",  # CP6.3: one provider generation (exact egress preview digest)
        "provider_egress",  # CP6.3: sending restricted/private inputs (exact payload digest)
    }
)

# Operations whose capability must be bound to an exact digest (a plan, a request, a payload).
# Both CP9 (overleaf_push) and CP6 (provider_*) add to it; a test pins the whole set.
DIGEST_REQUIRED = frozenset(
    {
        "exact_request",
        "purge",
        "overleaf_push",
        "provider_configure",
        "provider_run",
        "provider_egress",
    }
)


@dataclass(frozen=True)
class Grant:
    capability_id: str
    secret: str
    operation: str
    digest: str | None
    expires_at: str
    max_uses: int
    synthetic: bool


@dataclass(frozen=True)
class HeldGrant:
    """A server-held exact-plan grant (A02, CP10.2; decision 0016): the capability row an
    owner's grant of an approval request created. Its secret was discarded when it was
    issued, so it is presented by capability ID together with the approval request it is
    bound to. Only ``approval resume`` builds one, from the stored approval row; transports
    pass secrets as plain strings, which can never match a capability by ID. Consuming it
    also marks the approval request ``used`` in the same transaction."""

    capability_id: str
    approval_request_id: str


Secret = str | HeldGrant | None
"""What a protected operation is authorised with: a capability secret the client holds, a
server-held grant (approval resume), or nothing."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    operation: str,
    digest: str | None,
    ttl_seconds: int,
    max_uses: int,
    label: str,
    synthetic: bool = False,
) -> Grant:
    if operation not in OPERATIONS:
        raise PrometheosError("validation_error", "unknown capability operation", {})
    if operation in DIGEST_REQUIRED and not digest:
        raise PrometheosError("validation_error", f"{operation} capabilities need a digest", {})
    if not 1 <= max_uses <= 100 or not 30 <= ttl_seconds <= 86400:
        raise PrometheosError("validation_error", "capability limits out of range", {})
    secret = "pcap_" + secrets.token_urlsafe(32)
    capability_id = str(uuid.uuid4())
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO capabilities (id, secret_sha256, project_id, operation, digest, max_uses, "
        "uses, expires_at, policy_version, label, synthetic, created_at, revoked_at) "
        "VALUES (?,?,?,?,?,?,0,?,?,?,?,?,NULL)",
        (
            capability_id,
            _hash(secret),
            project_id,
            operation,
            digest,
            max_uses,
            expires,
            POLICY_VERSION,
            label,
            int(synthetic),
            utc_now(),
        ),
    )
    return Grant(capability_id, secret, operation, digest, expires, max_uses, synthetic)


def revoke(conn: sqlite3.Connection, capability_id: str) -> bool:
    cur = conn.execute(
        "UPDATE capabilities SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (utc_now(), capability_id),
    )
    return cur.rowcount == 1


def verify(
    conn: sqlite3.Connection,
    *,
    secret: Secret,
    project_id: str,
    operation: str,
    digest: str | None,
) -> str:
    """Check that ``secret`` would be accepted now, without counting a use (a pre-flight for
    operations that must not leave side effects when unauthorised). ``consume`` re-checks."""
    row = _validated(conn, secret, project_id, operation, digest)
    return str(row["id"])


def consume(
    conn: sqlite3.Connection,
    *,
    secret: Secret,
    project_id: str,
    operation: str,
    digest: str | None,
) -> str:
    """Validate and count one use inside the caller's write transaction.

    Raises ``policy_denied`` without revealing which property failed beyond what
    the owner needs; returns the capability ID for the audit row. A server-held grant also
    moves its approval request to ``used`` in this same transaction, so the protected
    mutation, the capability use and the approval's end commit together or not at all.
    """
    row = _validated(conn, secret, project_id, operation, digest)
    conn.execute("UPDATE capabilities SET uses = uses + 1 WHERE id = ?", (row["id"],))
    if isinstance(secret, HeldGrant):
        now = utc_now()[:19] + "Z"
        cur = conn.execute(
            "UPDATE approval_requests SET state = 'used', used_at = ?, closed_at = ? "
            "WHERE id = ? AND state = 'granted' AND capability_id = ?",
            (now, now, secret.approval_request_id, secret.capability_id),
        )
        if cur.rowcount != 1:  # re-checked by _validated; this is the write-side guard
            raise PrometheosError(
                "policy_denied",
                "the approval request is no longer granted",
                {"operation": operation},
            )
    return str(row["id"])


def _held_problem(conn: sqlite3.Connection, grant: HeldGrant) -> str | None:
    """Why the approval request bound to a server-held grant does not authorise now."""
    approval = conn.execute(
        "SELECT state, capability_id, expires_at FROM approval_requests WHERE id = ?",
        (grant.approval_request_id,),
    ).fetchone()
    if approval is None or approval["capability_id"] != grant.capability_id:
        return "unknown capability"
    if approval["state"] != "granted":
        return f"the approval request is {approval['state']}"
    if approval["expires_at"] <= utc_now()[:19] + "Z":
        return "the approval request expired"
    return None


def _validated(
    conn: sqlite3.Connection,
    secret: Secret,
    project_id: str,
    operation: str,
    digest: str | None,
) -> sqlite3.Row:
    if not secret:
        raise PrometheosError(
            "policy_denied",
            "this change needs an owner decision capability (prometheos decision review)",
            {"operation": operation},
        )
    if isinstance(secret, HeldGrant):
        held = _held_problem(conn, secret)
        if held:
            raise PrometheosError("policy_denied", held, {"operation": operation})
        row = conn.execute(
            "SELECT * FROM capabilities WHERE id = ?", (secret.capability_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM capabilities WHERE secret_sha256 = ?", (_hash(secret),)
        ).fetchone()
    now = utc_now()
    problem = None
    if row is None or row["project_id"] != project_id:
        problem = "unknown capability"
    elif row["revoked_at"] is not None:
        problem = "capability revoked"
    elif row["expires_at"] <= now[:19] + "Z":
        problem = "capability expired"
    elif row["uses"] >= row["max_uses"]:
        problem = "capability already used"
    elif row["policy_version"] != POLICY_VERSION:
        problem = "capability issued under a different policy version"
    elif row["operation"] != operation:
        problem = "capability does not cover this operation"
    elif row["digest"] is not None and row["digest"] != digest:
        problem = "capability was issued for a different proposal"
    if problem:
        raise PrometheosError("policy_denied", problem, {"operation": operation})
    assert row is not None
    found: sqlite3.Row = row
    return found


def describe(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, operation, digest, max_uses, uses, expires_at, label, synthetic, created_at, "
        "revoked_at FROM capabilities ORDER BY created_at"
    ).fetchall()
    return [json.loads(json.dumps(dict(r))) for r in rows]
