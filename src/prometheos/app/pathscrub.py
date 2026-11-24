"""The roots a reply to an agent or a browser must not show (decision 0012 items 43 and 47;
decision 0022 item 1).

One builder serves the hub's JSON middleware (:mod:`prometheos.hub.app`) and the MCP adapter
(:mod:`prometheos.mcp_adapter`), so a local path is removed the same way on both transports:
the rules live in :mod:`prometheos.hub.redaction` and :mod:`prometheos.domain.redact`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from prometheos.app import texengine
from prometheos.app.reads import Caller
from prometheos.audio import models
from prometheos.hub.redaction import Redactor, Roots, normalise_roots

ROOTS_READER = Caller(actor="path-roots", scope="owner")
"""Reads only the project's research-root aliases and paths, to label them."""


def build_redactor(service: Any, project_id: str, maintenance: bool = False) -> Redactor:
    """Roots for :mod:`prometheos.hub.redaction`. The managed folders are cheap to compute;
    the research roots are read only when an error or ``error`` field is rewritten.
    Maintenance mode uses the registry and known roots without opening a store or settings."""

    def managed() -> Roots:
        home = getattr(service, "home", None)
        pairs: list[tuple[Path | str | None, str]] = []
        for variable in (texengine.ENGINE_DIR_ENV, models.MODELS_DIR_ENV):
            if os.environ.get(variable):  # an override may lie outside the home
                pairs.append((os.environ[variable], f"<{variable}>"))
        if isinstance(home, Path):
            pairs.append((home, "<PrometheOS home>"))
            try:
                pairs.append((service.registry.lookup(project_id), "<project store>"))
            except Exception:  # noqa: S110 - an unreadable store still has the other roots
                pass
        return normalise_roots(pairs)

    def extra() -> Roots:
        pairs: list[tuple[Path | str | None, str]] = [
            (Path.home(), "<user home>"),
            (tempfile.gettempdir(), "<temp>"),
        ]
        if maintenance:
            return normalise_roots(pairs)
        try:
            settings = service.project_info(project_id, ROOTS_READER)["settings"]
            for root in settings.get("allowed_roots", []):
                pairs.append((root.get("path"), f"<research root {root.get('alias', '')}>"))
        except Exception:  # noqa: S110 - the other roots still apply
            pass
        return normalise_roots(pairs)

    return Redactor(managed, extra)
