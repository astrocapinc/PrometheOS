"""Test-only outbound-network guard for C11-NOACCOUNT (CP11 S11.5; decision 0069 2.2).

This file is never part of the product. ``tests/test_cp11_noaccount.py`` puts this folder first on
the ``PYTHONPATH`` of the hub and MCP server subprocesses it starts, so Python imports it at
start-up (``sitecustomize``). From then on, in that process:

- ``socket.socket.connect`` and ``connect_ex`` refuse (``ENETUNREACH``) and log every IPv4 or
  IPv6 address that is not loopback, and so do ``sendto`` and ``sendmsg`` with such an address
  (a UDP datagram is as outbound as a connection);
- ``socket.getaddrinfo`` refuses and logs every host name other than ``localhost``, so no DNS
  lookup leaves the computer and no connection by name is attempted;
- asyncio's own connect paths (``sock_connect``, ``create_connection``, which on Windows do not
  go through ``socket.connect``) are checked the same way.

Every refusal is one JSON line appended to the file named by ``NOACCOUNT_GUARD_LOG`` (kind,
target, process). A line ``{"kind": "installed"}`` is written when the guard loads, so a test can
tell "no attempt" from "guard not running". Loopback traffic (the hub's own port, a Zotero stub)
is not logged. A guard that is not asked for (no ``NOACCOUNT_GUARD_LOG``) does nothing.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from typing import Any

_LOG = os.environ.get("NOACCOUNT_GUARD_LOG")
_LOCK = threading.Lock()


def _write(kind: str, target: str) -> None:
    line = json.dumps(
        {
            "kind": kind,
            "target": target,
            "pid": os.getpid(),
            "argv": sys.argv[:3],
            "time": time.time(),
        }
    )
    with _LOCK, open(str(_LOG), "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _local_address(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None or host == "" or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).strip("[]").split("%")[0]).is_loopback
    except ValueError:
        return False


def _refuse(kind: str, target: str) -> OSError:
    _write(kind, target)
    return OSError(errno.ENETUNREACH, f"refused by the C11-NOACCOUNT guard: {kind} {target}")


def _install() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _local_address(address[0]):
            raise _refuse("connect", f"{address[0]}:{address[1]}")
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _local_address(address[0]):
            _write("connect_ex", f"{address[0]}:{address[1]}")
            return errno.ENETUNREACH
        return int(real_connect_ex(self, address))

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _local_address(host):
            _write("dns", str(host))
            raise socket.gaierror(socket.EAI_NONAME, "refused by the C11-NOACCOUNT guard")
        return real_getaddrinfo(host, *args, **kwargs)

    real_sendto = socket.socket.sendto
    real_sendmsg = getattr(socket.socket, "sendmsg", None)  # not on Windows

    def sendto(self: socket.socket, data: Any, *args: Any) -> Any:
        # sendto(data, address) or sendto(data, flags, address): the address is the last one
        address = args[-1] if args else None
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple)
            and not _local_address(address[0])
        ):
            raise _refuse("sendto", f"{address[0]}:{address[1]}")
        return real_sendto(self, data, *args)

    def sendmsg(self: socket.socket, buffers: Any, *args: Any) -> Any:
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        address = args[2] if len(args) > 2 else None
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple)
            and not _local_address(address[0])
        ):
            raise _refuse("sendmsg", f"{address[0]}:{address[1]}")
        assert real_sendmsg is not None
        return real_sendmsg(self, buffers, *args)

    socket.socket.sendto = sendto  # type: ignore[method-assign]
    if real_sendmsg is not None:
        socket.socket.sendmsg = sendmsg  # type: ignore[method-assign]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        import asyncio.base_events as base_events

        loop_class = base_events.BaseEventLoop
        real_sock_connect = loop_class.sock_connect

        async def sock_connect(self: Any, sock: socket.socket, address: Any) -> Any:
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local_address(address[0]):
                raise _refuse("sock_connect", f"{address[0]}:{address[1]}")
            return await real_sock_connect(self, sock, address)

        loop_class.sock_connect = sock_connect  # type: ignore[method-assign]
        real_create = loop_class.create_connection

        async def create_connection(
            self: Any, factory: Any, host: Any = None, *a: Any, **k: Any
        ) -> Any:
            if host is not None and not _local_address(host):
                raise _refuse("create_connection", str(host))
            return await real_create(self, factory, host, *a, **k)

        loop_class.create_connection = create_connection  # type: ignore[method-assign]
        if sys.platform == "win32":
            import asyncio.proactor_events as proactor

            proactor_class = proactor.BaseProactorEventLoop
            real_proactor_connect = proactor_class.sock_connect

            async def proactor_connect(self: Any, sock: socket.socket, address: Any) -> Any:
                if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local_address(
                    address[0]
                ):
                    raise _refuse("sock_connect", f"{address[0]}:{address[1]}")
                return await real_proactor_connect(self, sock, address)

            proactor_class.sock_connect = proactor_connect  # type: ignore[method-assign]
    except Exception as exc:  # the socket-level guard above still holds
        _write("asyncio_patch_failed", type(exc).__name__)
    _write("installed", "")


if _LOG:
    _install()
