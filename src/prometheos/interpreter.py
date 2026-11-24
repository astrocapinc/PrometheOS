"""How PrometheOS starts its own interpreter again: the plugin's MCP server and hook (written
into a coding host's configuration) and the run worker (a child of a running PrometheOS).

``-I`` (isolated mode) keeps the user's own site-packages and ``PYTHON*`` variables out, so a
package another tool installed for the same Python minor version can never shadow the one
PrometheOS ships: the bundled core is a plain CPython, and an older ``mcp`` in the user site
once stopped the plugin's MCP server at import (dogfood P9). ``-X utf8`` makes text output
UTF-8 whatever the console code page is (dogfood P10). The desktop app starts its sidecar
with the same flags.
"""

from __future__ import annotations

import sys

PYTHON_FLAGS: tuple[str, ...] = ("-I", "-X", "utf8")
"""For a command a coding host runs: nothing from the host's environment reaches the import
path (the host's ``PYTHONPATH`` is not PrometheOS's)."""

CHILD_FLAGS: tuple[str, ...] = ("-s", "-X", "utf8")
"""For a child of a running PrometheOS: the user site stays out, but ``PYTHONPATH`` is kept, so
the child imports the same PrometheOS as its parent (a development checkout on PYTHONPATH,
review kers-p9-a F2)."""


def child_flags() -> tuple[str, ...]:
    """The flags for a child of this process: isolated when this process is (a host started it
    with PYTHON_FLAGS), else CHILD_FLAGS, so a child never sees more of the environment than its
    parent did (review kers-p9-a-r2 R2-4)."""
    return PYTHON_FLAGS if sys.flags.isolated else CHILD_FLAGS
