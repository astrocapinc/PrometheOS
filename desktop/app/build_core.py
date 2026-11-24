"""Build the PrometheOS core that the desktop installer ships (CP19.2, decision 0054).

Usage (repository root, main environment):
    .venv/Scripts/python.exe desktop/app/build_core.py

Writes ``desktop/app/build/core`` (git-ignored), which electron-builder copies into the app's
``resources/core``:
- CPython 3.14.2, the version the project is tested with: python-build-standalone, installed by
  uv (checksum-verified) into the git-ignored ``local/toolchains/python``, without tests,
  IDLE, Tk, headers or import libraries;
- PrometheOS's locked runtime dependencies and the ``adoption-parsers`` extra (tree-sitter and
  tree-sitter-matlab, both MIT; decision 0072, so the app parses MATLAB files when it adopts a
  folder), from ``uv export --no-dev --extra adoption-parsers``, installed offline from the uv
  cache into ``Lib/site-packages``;
- the prometheos wheel built from this tree.
Then it checks that the bundled interpreter runs ``python -m prometheos --version`` in
isolation from this machine's environment, and writes ``MANIFEST.json`` (versions, file
count, size and the SHA-256 of the interpreter and the wheel).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent
REPO = APP.parents[1]
PYTHON_HOME = REPO / "local" / "toolchains" / "python" / "cpython-3.14.2-windows-x86_64-none"
OUT = APP / "build" / "core"
SKIP_TOP = {"include", "libs", "tcl", "Scripts", "share"}
SKIP_LIB = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "site-packages"}
# DLL-folder files the app never loads: Tk and CPython's own test modules (review s3c2-a F5).
SKIP_DLLS = ("tcl", "tk", "_tkinter", "_test", "_ctypes_test")
# The optional extras the desktop core ships (decision 0072: MATLAB parsing for adoption).
EXTRAS = ("--extra", "adoption-parsers")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], **kwargs: object) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, **kwargs)  # type: ignore[call-overload]
    if done.returncode != 0:
        raise SystemExit(f"failed: {' '.join(argv)}\n{done.stdout}\n{done.stderr}")
    return str(done.stdout)


def ignore(directory: str, names: list[str]) -> set[str]:
    here = Path(directory)
    if here == PYTHON_HOME:
        return {n for n in names if n in SKIP_TOP}
    if here == PYTHON_HOME / "Lib":
        return {n for n in names if n in SKIP_LIB}
    if here == PYTHON_HOME / "DLLs":
        return {n for n in names if n.lower().startswith(SKIP_DLLS)}
    return {n for n in names if n == "__pycache__"}


def strip_build_paths(core: Path) -> None:
    """Remove what records this build machine's paths (review publish-ready-a F1): PEP 610
    ``direct_url.json`` (a ``file:`` URL of the wheel's temporary folder) with its RECORD line,
    and any bytecode."""
    for direct in (core / "Lib" / "site-packages").glob("*.dist-info/direct_url.json"):
        record = direct.parent / "RECORD"
        if record.is_file():
            lines = record.read_text("utf-8").splitlines(keepends=True)
            entry = f"{direct.parent.name}/direct_url.json,"
            kept = [line for line in lines if not line.startswith(entry)]
            record.write_text("".join(kept), "utf-8")
        direct.unlink()
    # Also the standard library's: uv runs this interpreter to inspect it while installing.
    for cache in [p for p in core.rglob("__pycache__") if p.is_dir()]:
        shutil.rmtree(cache)


def main() -> int:
    if not (PYTHON_HOME / "python.exe").is_file():
        raise SystemExit(
            f"missing {PYTHON_HOME}: install it with UV_PYTHON_INSTALL_DIR=local/toolchains/python "
            "uv python install cpython-3.14.2-windows-x86_64-none --no-bin"
        )
    pin = json.loads((APP / "core-pin.json").read_text("utf-8"))
    for name, key in (("python.exe", "python_exe_sha256"), ("python314.dll", "python_dll_sha256")):
        if sha256(PYTHON_HOME / name) != pin[key]:
            raise SystemExit(f"the local CPython's {name} does not match desktop/app/core-pin.json")
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(PYTHON_HOME, OUT, ignore=ignore)
    python = OUT / "python.exe"
    with tempfile.TemporaryDirectory(prefix="pcore-") as tmp:
        work = Path(tmp)
        requirements = work / "requirements.txt"
        # With the lock's hashes: every wheel installed is the one the lock names (F6).
        run(["uv", "export", "--no-dev", "--no-emit-project", "--frozen", *EXTRAS,
             "--format", "requirements-txt", "-o", str(requirements)], cwd=REPO)  # fmt: skip
        # Pinned build closure (decision 0057): an offline build must not float.
        run(["uv", "build", "--wheel", "--offline", "--build-constraints",
             "build-constraints.txt", "--out-dir", str(work / "wheel")], cwd=REPO)  # fmt: skip
        (wheel,) = (work / "wheel").glob("prometheos-*.whl")
        site = OUT / "Lib" / "site-packages"
        run(["uv", "pip", "install", "--offline", "--no-deps", "--require-hashes", "--target",
             str(site), "--python", str(python), "-r", str(requirements)])  # fmt: skip
        run(["uv", "pip", "install", "--offline", "--no-deps", "--target", str(site),
             "--python", str(python), str(wheel)])  # fmt: skip
        # Console launchers of the installed packages (site-packages/bin) point at this build
        # machine's paths and are never run by the app (F5).
        shutil.rmtree(site / "bin", ignore_errors=True)
        wheel_sha = sha256(wheel)
        wheel_name = wheel.name
    # The bundled interpreter must run the app with nothing from this machine's environment.
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
    # -B: no bytecode, whose co_filename would carry this build machine's paths (review
    # publish-ready-a F1); the installed app writes its own on first use.
    version = run([str(python), "-I", "-B", "-m", "prometheos", "--version"], env=clean).strip()
    strip_build_paths(OUT)
    files = [p for p in OUT.rglob("*") if p.is_file()]
    manifest = {
        "python": run(
            [str(python), "-I", "-B", "-c", "import sys; print(sys.version.split()[0])"], env=clean
        ).strip(),
        "prometheos": version,
        "wheel": wheel_name,
        "wheel_sha256": wheel_sha,
        "python_exe_sha256": sha256(python),
        "files": len(files),
        "mib": round(sum(p.stat().st_size for p in files) / 1048576, 1),
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
