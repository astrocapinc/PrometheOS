"""Print the actual test environment as JSON (operating system, runtimes, tool versions)."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def tool(argv: list[str]) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0] if out.returncode == 0 and text else None


def dist(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def where(path: Path, root: Path) -> str:
    """``path`` relative to the candidate root, or a marker naming only its last part (no
    absolute host paths in evidence)."""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return f"<outside the candidate>/{path.name}"


def code_under_test(root: Path) -> dict[str, object]:
    """Which code this interpreter tests (decision 0051): the session-10 launcher selected a
    worktree's source through PYTHONPATH while the records named another checkout."""
    try:
        import prometheos

        package: str | None = where(Path(prometheos.__file__).parent, root)
    except ImportError:
        package = None
    entries = [e for e in os.environ.get("PYTHONPATH", "").split(os.pathsep) if e]
    return {
        "prometheos_package": package,
        "package_in_candidate": package == "src/prometheos",
        "environment": where(Path(sys.prefix), root),
        "pythonpath": [where(Path(e), root) for e in entries],
    }


def main() -> int:
    tools = {
        "sqlite": sqlite3.sqlite_version,
        "uv": tool(["uv", "--version"]),
        "git": tool(["git", "--version"]),
        "node": tool(["node", "--version"]),
    }
    for name in ("prometheos", "pydantic", "typer", "pytest", "ruff", "mypy", "jsonschema"):
        tools[name] = dist(name)
    report = {
        "os": f"{platform.system()} {platform.release()} {platform.version()}",
        "architecture": platform.machine(),
        "python": sys.version.split()[0],
        "python_executable_prefix": sys.prefix.replace("\\", "/").rsplit("/", 1)[-1],
        "tool_versions": [{"name": k, "version": v} for k, v in tools.items() if v],
        "code_under_test": code_under_test(Path.cwd().resolve()),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
