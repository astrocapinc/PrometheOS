"""C00-BOOT: build the wheel and install it into fresh minimal environments.

For each requested interpreter: create an empty virtual environment, install the
lockfile's exact runtime pins (no development group, no extras) plus the built
wheel, then run ``prometheos --help`` and ``--version`` from an empty working
directory with credential-like variables removed and a temporary
PROMETHEOS_HOME. Verifies no files are created, development tools are absent
and the reported version matches the wheel.

Usage: uv run python tools/clean_install_check.py [--python 3.12 --python 3.14]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(argv), flush=True)
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", **kwargs)  # type: ignore[call-overload]
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc


def check_python(python: str, wheel: Path, requirements: Path, work: Path) -> list[str]:
    failures: list[str] = []
    venv = work / f"venv-{python}"
    if run(["uv", "venv", "--python", python, "--no-project", str(venv)]).returncode != 0:
        return [f"{python}: could not create virtual environment"]
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    interpreter = scripts / ("python.exe" if os.name == "nt" else "python")
    install = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--no-deps",
            "-r",
            str(requirements),
            str(wheel),
        ]
    )
    if install.returncode != 0:
        return [f"{python}: install failed"]
    probe = run(
        [
            str(interpreter),
            "-c",
            "import importlib.util as u; print([m for m in ('pytest','mypy','ruff')"
            " if u.find_spec(m)])",
        ]
    )
    if probe.stdout.strip() != "[]":
        failures.append(f"{python}: development tools present in runtime install: {probe.stdout}")
    empty = work / f"cwd-{python}"
    empty.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "VIRTUAL_ENV"))
    }
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    env["PROMETHEOS_HOME"] = str(work / f"home-{python}")
    exe = scripts / ("prometheos.exe" if os.name == "nt" else "prometheos")
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    utf8_env = dict(env, PYTHONIOENCODING="utf-8")
    for args, run_env in ((["--help"], env), (["--help"], utf8_env), (["--version"], env)):
        out = run([str(exe), *args], cwd=empty, env=run_env, stdin=subprocess.DEVNULL, timeout=120)
        if out.returncode != 0:
            failures.append(f"{python}: prometheos {' '.join(args)} exited {out.returncode}")
        if args == ["--version"] and out.stdout.strip() != f"prometheos {expected}":
            failures.append(f"{python}: unexpected version output {out.stdout!r}")
        if args == ["--help"] and "Usage" not in out.stdout:
            failures.append(f"{python}: help output missing usage")
    if any(empty.iterdir()):
        failures.append(f"{python}: help/version created files in the working directory")
    if Path(env["PROMETHEOS_HOME"]).exists():
        failures.append(f"{python}: help/version created an application home")
    return failures


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fresh minimal install check.")
    parser.add_argument("--python", action="append", default=None)
    args = parser.parse_args(argv)
    pythons = args.python or ["3.12", "3.14"]
    work = Path(tempfile.mkdtemp(prefix="prometheos-clean-"))
    failures: list[str] = []
    try:
        dist = work / "dist"
        # Pinned build closure (decision 0057).
        build = run(
            ["uv", "build", "--wheel", "--build-constraints", "build-constraints.txt",
             "--out-dir", str(dist)],
            cwd=ROOT,
        )  # fmt: skip
        wheels = sorted(dist.glob("prometheos-*.whl"))
        if build.returncode != 0 or len(wheels) != 1:
            print("FAIL: wheel build failed", file=sys.stderr)
            return 1
        shipped = ROOT / "src" / "prometheos" / "hub" / "static"
        if not (shipped / "build-info.json").is_file():
            failures.append("hub assets are not built (run tools/build_hub.py)")
        else:
            import json as _json
            import zipfile

            info = _json.loads((shipped / "build-info.json").read_text(encoding="utf-8"))
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
            for needed in ["build-info.json", *info["files"]]:
                if f"prometheos/hub/static/{needed}" not in names:
                    failures.append(f"wheel is missing hub asset {needed}")
        requirements = work / "runtime-requirements.txt"
        export = run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
        )
        if export.returncode != 0:
            print("FAIL: could not export locked runtime requirements", file=sys.stderr)
            return 1
        for python in pythons:
            failures.extend(check_python(python, wheels[0], requirements, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if not failures:
        print(f"PASS: clean wheel install, help and version on Python {', '.join(pythons)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
