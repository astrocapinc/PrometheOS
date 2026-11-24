"""Loopback hub access control (spec/08-operations.md).

* Bound to 127.0.0.1 only; every request must carry an expected Host (DNS-rebinding
  defence) and, when present, an exact same-origin Origin. Mutations require Origin.
* A one-use, five-minute, high-entropy pairing token is printed by the launching CLI
  and exchanged in a POST body (never a URL). Only its hash is kept in memory.
* A paired session is an HttpOnly, SameSite=Strict cookie with a short absolute
  lifetime; mutations also need the session's CSRF token in a header. Sessions live in
  memory only: restarting the hub signs everyone out.
* Responses carry a restrictive CSP, frame denial and no-store caching for the API.
* A local coding agent may be given a bearer token (A02, CP10.2): it reaches only the
  agent-scoped routes, at agent scope. A browser cannot attach that header to a cross-site
  request without a CORS preflight, which the hub never grants, so an agent request needs
  no Origin; when one is sent it must still be exact.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "prometheos_hub"
CSRF_HEADER = "x-prometheos-csrf"
PAIRING_TTL = 300.0
SESSION_TTL = 8 * 3600.0
AGENT_TOKEN_TTL = 8 * 3600.0  # like a paired session: a new token needs a new hub start
MAX_PAIRING_FAILURES = 10
SAFE_METHODS = frozenset({"GET", "HEAD"})
CSP = (
    "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; media-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; manifest-src 'self'; "
    "form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _Session:
    csrf: str
    expires: float
    device_id: str | None = None  # A07, CP21.2: the remembered device that resumed it


@dataclass
class Gate:
    """In-memory pairing and session state for one hub process."""

    port: int
    clock: object = field(default=time.monotonic)
    # A07, CP21.2: the home's own host name (``h<16 hex>.localhost``), where the remembered
    # device credential lives; 127.0.0.1 and localhost stay for other clients.
    home_host: str | None = None
    _pairing: str | None = None
    _pairing_expires: float = 0.0
    _failures: int = 0
    _sessions: dict[str, _Session] = field(default_factory=dict)
    _agent_tokens: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    # ----- origin and host --------------------------------------------------------------

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.home_host:
            hosts.add(f"{self.home_host}:{self.port}")
        return frozenset(hosts)

    def is_home_host(self, host: str | None) -> bool:
        return bool(self.home_host) and (host or "").lower() == f"{self.home_host}:{self.port}"

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)

    def host_ok(self, host: str | None) -> bool:
        return host is not None and host.lower() in self.allowed_hosts

    def origin_ok(self, method: str, origin: str | None, fetch_site: str | None) -> bool:
        if origin is not None:
            return origin in self.allowed_origins
        if method in SAFE_METHODS:
            # Browsers omit Origin on same-origin navigations/GETs; refuse explicit
            # cross-site fetch metadata anyway.
            return fetch_site in (None, "same-origin", "none")
        return False  # every mutation must state its origin

    # ----- pairing ----------------------------------------------------------------------

    def new_pairing_token(self, ttl: float = PAIRING_TTL) -> str:
        """A one-use pairing token. The launcher's launch codes (A07, CP21) are the same
        token with a 60-second ``ttl``, issued only over the owner channel."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._pairing = _digest(token)
            self._pairing_expires = self._now() + ttl
            self._failures = 0
        return token

    def pair(self, token: str) -> tuple[str, str] | None:
        """Exchange the pairing token for (session id, csrf token); one use only."""
        with self._lock:
            expected = self._pairing
            if expected is None or self._now() > self._pairing_expires:
                self._pairing = None
                return None
            if not hmac.compare_digest(_digest(token), expected):
                self._failures += 1
                if self._failures >= MAX_PAIRING_FAILURES:
                    self._pairing = None  # stop guessing; the owner starts a new pairing
                return None
            self._pairing = None
        return self.new_session()

    def new_session(self, device_id: str | None = None) -> tuple[str, str]:
        """A new owner session (session id, csrf token): after a pairing, or after a remembered
        device credential was exchanged (A07, CP21.2)."""
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_digest(session_id)] = _Session(
                csrf, self._now() + SESSION_TTL, device_id
            )
        return session_id, csrf

    def bind_device(self, session_id: str, device_id: str) -> None:
        """Tie a pairing's session to the device it remembered, so revoking that device ends
        it too (review cp21-a F3)."""
        with self._lock:
            found = self._sessions.get(_digest(session_id))
            if found is not None:
                found.device_id = device_id

    def home_origin(self) -> str | None:
        """The one Origin a remembered device credential is accepted with (spec/08)."""
        return f"http://{self.home_host}:{self.port}" if self.home_host else None

    def end_device(self, device_id: str) -> int:
        """End every live session a revoked device resumed."""
        with self._lock:
            ended = [k for k, s in self._sessions.items() if s.device_id == device_id]
            for key in ended:
                del self._sessions[key]
        return len(ended)

    def session(self, session_id: str | None) -> _Session | None:
        if not session_id:
            return None
        with self._lock:
            key = _digest(session_id)
            found = self._sessions.get(key)
            if found is None:
                return None
            if self._now() > found.expires:
                del self._sessions[key]
                return None
            return found

    def csrf_ok(self, session: _Session, header: str | None) -> bool:
        return header is not None and hmac.compare_digest(header, session.csrf)

    def end(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(_digest(session_id), None)

    # ----- agent access (A02, CP10.2; decision 0016) ------------------------------------

    def new_agent_token(self) -> str:
        """A bearer token for a local coding agent (``Authorization: Bearer <token>``). It
        reaches only the agent-scoped routes (``hub.app.AGENT_ROUTES``) and acts at agent
        scope; owner-channel routes and the changes long-poll stay paired-session only. Only
        its hash is kept; it lives as long as this hub process."""
        token = "pagt_" + secrets.token_urlsafe(32)
        with self._lock:
            self._agent_tokens[_digest(token)] = self._now() + AGENT_TOKEN_TTL
        return token

    def agent_ok(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        presented = _digest(authorization.removeprefix("Bearer ").strip())
        now = self._now()
        with self._lock:
            for known, expires in list(self._agent_tokens.items()):
                if now > expires:
                    del self._agent_tokens[known]  # an expired token is gone for good
            return any(hmac.compare_digest(presented, known) for known in self._agent_tokens)
