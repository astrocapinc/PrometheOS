"""Provider configuration, routing policy and route classification (decision 0013 items 13-15).

One per-project configuration (stored in ``provider_settings``, not in the sealed project
settings): a routing policy, an explicit endpoint and model, a credential *reference*, owner
supplied prices and budgets. Nothing here holds a secret value.

Policies: ``no-ai`` (default: nothing generated), ``local-only`` (the endpoint must be this
computer) and ``remote-allowed``. A route is ``local`` for a loopback endpoint and ``remote``
for anything else; there is no fallback from one route to the other.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from prometheos.domain.canonical import canonical_sha256, dumps_canonical, loads_internal
from prometheos.domain.models import PrometheosError, StrictModel, utc_now
from prometheos.store import db
from prometheos.store.project import ProjectStore

Policy = Literal["no-ai", "local-only", "remote-allowed"]
Capability = Literal["generation", "embedding", "transcription", "classification"]
CAPABILITIES: tuple[str, ...] = ("generation", "embedding", "transcription", "classification")
CredentialRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
DecimalText = Annotated[str, StringConstraints(pattern=r"^\d{1,6}(\.\d{1,6})?$")]
LOOPBACK_NAMES = frozenset({"localhost"})


def host_of(endpoint: str) -> str:
    return (urlsplit(endpoint).hostname or "").lower()


def is_loopback_host(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def route_of(endpoint: str | None) -> str:
    """``local`` (loopback endpoint), ``remote`` (anything else) or ``none``."""
    if not endpoint:
        return "none"
    return "local" if is_loopback_host(host_of(endpoint)) else "remote"


class Budget(StrictModel):
    max_input_chars: int = Field(ge=1000, le=400_000)
    max_output_tokens: int = Field(ge=64, le=16384)
    max_calls_per_run: int = Field(ge=1, le=2)  # one generation plus at most one repair
    spend_cap_usd: DecimalText | None
    timeout_seconds: int = Field(ge=5, le=300)


class ProviderConfig(StrictModel):
    policy: Policy
    endpoint: Annotated[str, StringConstraints(max_length=512)] | None
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None
    credential_ref: CredentialRef | None
    price_input_per_mtok_usd: DecimalText | None
    price_output_per_mtok_usd: DecimalText | None
    budget: Budget

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("endpoint must be an http(s) URL with a host")
        if parts.username or parts.password or "@" in parts.netloc:
            raise ValueError("endpoint must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("endpoint must not carry a query or fragment")
        if parts.scheme == "http" and not is_loopback_host(parts.hostname.lower()):
            raise ValueError("a remote endpoint must use https")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _consistent(self) -> ProviderConfig:
        if self.policy != "no-ai" and (self.endpoint is None or self.model is None):
            raise ValueError("a policy that allows AI needs an explicit endpoint and model")
        route = route_of(self.endpoint)
        if self.policy == "local-only" and route != "local":
            raise ValueError("local-only needs a loopback endpoint (localhost, 127.0.0.1, ::1)")
        if route == "remote" and self.credential_ref is None:
            raise ValueError("a remote endpoint needs a credential reference")
        if self.budget.spend_cap_usd is not None and (
            self.price_input_per_mtok_usd is None or self.price_output_per_mtok_usd is None
        ):
            raise ValueError("a spend cap needs the model's input and output prices")
        return self

    @property
    def route(self) -> str:
        return route_of(self.endpoint)


def default_config() -> ProviderConfig:
    return ProviderConfig(
        policy="no-ai",
        endpoint=None,
        model=None,
        credential_ref=None,
        price_input_per_mtok_usd=None,
        price_output_per_mtok_usd=None,
        budget=Budget(
            max_input_chars=60_000,
            max_output_tokens=2048,
            max_calls_per_run=2,
            spend_cap_usd=None,
            timeout_seconds=60,
        ),
    )


def parse(document: Any) -> ProviderConfig:
    from pydantic import ValidationError

    try:
        loads_internal(dumps_canonical(document))
        return ProviderConfig.model_validate(document, strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        raise PrometheosError(
            "validation_error",
            "provider configuration is invalid",
            {"errors": str(exc)[:1500]},
        ) from exc


def digest(config: ProviderConfig) -> str:
    return canonical_sha256(config.model_dump(mode="json"))


def load(conn: sqlite3.Connection) -> tuple[ProviderConfig, int]:
    """The stored configuration and its revision (0 means never configured: the default)."""
    row = conn.execute(
        "SELECT revision, config FROM provider_settings WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return default_config(), 0
    return parse(json.loads(row["config"])), int(row["revision"])


def save(
    store: ProjectStore,
    config: ProviderConfig,
    base_revision: int,
    authorize: Callable[[sqlite3.Connection], None] | None = None,
) -> int:
    """Store ``config`` if the revision is still ``base_revision``; returns the new revision.

    ``authorize`` runs inside the same transaction (the owner's capability is spent only if
    the change is stored)."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            if authorize is not None:
                authorize(conn)
            _, current = load(conn)
            if current != base_revision:
                raise PrometheosError(
                    "revision_conflict",
                    "the provider configuration changed; review it again",
                    {"revision": current},
                )
            text = dumps_canonical(config.model_dump(mode="json")).decode("utf-8")
            if current == 0:
                conn.execute(
                    "INSERT INTO provider_settings (singleton, revision, config, updated_at) "
                    "VALUES (1, 1, ?, ?)",
                    (text, utc_now()),
                )
            else:
                conn.execute(
                    "UPDATE provider_settings SET revision = ?, config = ?, updated_at = ? "
                    "WHERE singleton = 1",
                    (current + 1, text, utc_now()),
                )
            return current + 1
    finally:
        conn.close()


def price_per_token(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value) / Decimal(1_000_000)
