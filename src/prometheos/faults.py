"""Named crash points and in-process injection points for recovery probes.

When ``PROMETHEOS_FAULT_POINT`` equals a point's name the process terminates
immediately with ``os._exit(86)``: no ``finally`` blocks, no flushing, no
transaction cleanup -- the closest in-process analogue of being killed. Tests
use disposable stores only. Unset in normal operation, this is a no-op.

A test may also register a hook for a point (:func:`inject`): it runs, in the calling
thread, each time execution reaches that point. This is how a test makes something happen at
an exact moment of an operation (a purge in the middle of a transcription, for example).
Nothing registers hooks outside tests.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

FAULT_ENV = "PROMETHEOS_FAULT_POINT"
FAULT_EXIT_CODE = 86

_HOOKS: dict[str, Callable[[], None]] = {}


def fault_point(name: str) -> None:
    hook = _HOOKS.get(name)
    if hook is not None:
        hook()
    if os.environ.get(FAULT_ENV) == name:
        os._exit(FAULT_EXIT_CODE)


@contextmanager
def inject(name: str, hook: Callable[[], None]) -> Iterator[None]:
    """Run ``hook`` every time execution reaches the fault point ``name`` (tests only)."""
    previous = _HOOKS.get(name)
    _HOOKS[name] = hook
    try:
        yield
    finally:
        if previous is None:
            _HOOKS.pop(name, None)
        else:
            _HOOKS[name] = previous
