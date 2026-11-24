"""HTTP transport behind a small interface.

``UrllibTransport`` is the only code that opens a socket to a provider; tests use a scripted
transport. A failure is classified by what it says about the request: ``NotSent`` (nothing
left this computer: refused, unresolved host, certificate failure before the request) or
``OutcomeUnknown`` (the request may have been received: timeout, reset after sending). The
caller never retries an unknown outcome on its own. Redirects are not followed, so a
credential header can never be forwarded to another host.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes


class NotSent(Exception):
    """Nothing was transmitted."""


class OutcomeUnknown(Exception):
    """The request may have reached the provider; whether it was billed is unknown."""


class Transport(Protocol):
    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply: ...


class ReadingTransport(Transport, Protocol):
    """A transport that can also read (the layer reads the key's remaining limit, A07.6)."""

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Reply: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class UrllibTransport:
    def get(self, url: str, headers: dict[str, str], timeout: float) -> Reply:
        """A read (no body), with the same rules as ``post``: no proxy, no redirect."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
        try:
            with opener.open(request, timeout=timeout) as response:
                return Reply(response.status, response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as err:
            try:
                return Reply(err.code, err.read(MAX_RESPONSE_BYTES + 1))
            finally:
                err.close()
        except urllib.error.URLError as err:
            raise NotSent(type(err.reason).__name__) from None
        except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as err:
            raise OutcomeUnknown(type(err).__name__) from None

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        # No environment or registry proxy for any route: the network path is the endpoint the
        # preview declared, never a proxy that would make a local route leave the machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
        try:
            with opener.open(request, timeout=timeout) as response:
                return Reply(response.status, response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as err:
            try:
                return Reply(err.code, err.read(MAX_RESPONSE_BYTES + 1))
            finally:
                err.close()
        except urllib.error.URLError as err:
            reason = err.reason
            if isinstance(reason, ConnectionRefusedError | socket.gaierror):
                raise NotSent(type(reason).__name__) from None
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise NotSent("certificate verification failed") from None
            raise OutcomeUnknown(type(reason).__name__) from None
        except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as err:
            raise OutcomeUnknown(type(err).__name__) from None
