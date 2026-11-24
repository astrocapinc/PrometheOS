"""Shared helpers for CP9 tests: a synthetic LaTeX workspace inside a disposable project.

The real-engine tests use the Tectonic installed by `prometheos tex engine install`
(``PROMETHEOS_TEX_ENGINE_DIR``, else the default home's ``tools/tectonic-0.17.0``). They
are marked ``tectonic``; see ``conftest.py`` for how a missing engine fails loudly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from cp1_support import OWNER, Env
from cp7_support import create, paper_body

from prometheos.app import texengine
from prometheos.domain.ids import new_id

LATEX_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "latex" / "basic"


def default_engine_dir() -> Path | None:
    configured = os.environ.get(texengine.ENGINE_DIR_ENV)
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "PrometheOS-v1" / "tools" / f"tectonic-{texengine.TECTONIC.version}"


def engine_problem() -> str | None:
    """Why the real engine cannot be used, or None when it is installed and verified."""
    directory = default_engine_dir()
    if directory is None:
        return "no engine folder (set PROMETHEOS_TEX_ENGINE_DIR)"
    previous = os.environ.get(texengine.ENGINE_DIR_ENV)
    os.environ[texengine.ENGINE_DIR_ENV] = str(directory)
    try:
        report = texengine.status(Path("unused-home"))
    finally:
        if previous is None:
            os.environ.pop(texengine.ENGINE_DIR_ENV, None)
        else:
            os.environ[texengine.ENGINE_DIR_ENV] = previous
    if report["state"] != "installed":
        return f"engine at {directory} is {report['state']}: {report['problem']}"
    if report["cache"]["files"] == 0:
        return f"engine at {directory} has an empty bundle cache (compile once with --fetch)"
    return None


class LatexEnv:
    """Env with the synthetic LaTeX fixture copied to ``research/paper`` and a paper."""

    def __init__(self, env: Env, folder: str = "paper") -> None:
        self.env = env
        self.svc = env.svc
        self.pid = env.project_id
        self.folder = env.research / folder
        shutil.copytree(LATEX_FIXTURE, self.folder)
        self.paper_id = new_id("paper")
        env.apply([create("paper", paper_body(title="Synthetic workspace paper"), self.paper_id)])
        self.bound = self.svc.workspace_bind(
            self.pid, OWNER, self.paper_id, "research", f"{folder}/main.tex"
        )

    def path(self, rel: str) -> Path:
        return self.folder / rel

    def read(self, rel: str, caller: Any = OWNER) -> dict[str, Any]:
        return self.svc.workspace_read(self.pid, caller, self.paper_id, rel)

    def write(self, rel: str, text: str, base: str | None, caller: Any = OWNER) -> dict[str, Any]:
        return self.svc.workspace_write(self.pid, caller, self.paper_id, rel, text, base)

    def get(self, rid: str) -> dict[str, Any]:
        return self.svc.get(self.pid, OWNER, rid, None)

    def paper(self) -> dict[str, Any]:
        return self.get(self.paper_id)
