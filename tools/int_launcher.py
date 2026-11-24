"""Run recorded checks and integration runs against an explicit worktree (decision 0051).

Replaces the session-10 scratch launcher (``scratch/claude-s10/int5/main_check.py`` and
``run_all.ps1``). The target is always named, and everything that decides which code is tested
comes from it:
- its own ``tools/run_check.py`` and ``tools/run_checkpoint.py`` run in it (``cwd`` = target);
- its own ``.venv`` is the uv project environment, first on PATH, with the project installed
  editable from its own ``src``; no PYTHONPATH is set, and the environment report of every
  checkpoint run states where ``prometheos`` was imported from (``code_under_test``);
- a relative run folder is resolved against the target, never against the launcher's checkout;
- no ``--basetemp`` is appended: TEMP is a short synthetic folder, so the default from decision
  0048 (``%TEMP%\\ptb\\<pid>``) stays short.

Each invocation writes ``launcher.json`` next to its records: the target, its HEAD, branch and
dirty state, its tree digest before and after, the environment it was given and this file's
digest.

The host-specific paths (uv, Node, Git, staged offline components, the Playwright cache, the
real uv cache) come from a JSON configuration, by default ``local/integration-launcher.json`` in
the main checkout (git-ignored). Keys: see ``CONFIG_KEYS``.

Usage:
  python tools/int_launcher.py check --target <worktree> --run-dir <dir> --name <name> -- <cmd...>
  python tools/int_launcher.py integration --target <worktree> --suffix <s>
      [--only CP18,CP0] [--reuse-from <run folder>,...] [--extra audio ...]

``pytest`` / ``python`` as the first word of a ``check`` command run under the target's venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
CONFIG_KEYS = {
    "uv": "uv executable",
    "node_dir": "folder holding node.exe and npm",
    "git_cmd_dir": "Git for Windows cmd folder",
    "uv_cache_dir": "the real uv cache (offline wheel builds read it)",
    "playwright_browsers_path": "the per-user Playwright browser cache",
    "tectonic_dir": "staged Tectonic engine folder (PROMETHEOS_TEX_ENGINE_DIR)",
    "models_dir": "staged speech models folder (PROMETHEOS_MODELS_DIR)",
}
OPTIONAL_KEYS = {"temp_root": "parent of the short synthetic environments (default: %TEMP%)"}
DEFAULT_ORDER = ["CP18", "CP0", "CP1", "CP2", "CP3", "CP4", "CP5", "CP7", "CP6", "CP9", "CP10"]
ENV_MARKER = ".prometheos-launcher"
ENV_KEEP_SECONDS = 86400
# Decision 0048's default basetemp under the synthetic TEMP is ``<TEMP>\ptb\<pid>`` and xdist adds
# ``\popen-gwNN``. pytest's own default (77 characters here) left test_cp10_mcp_paths past
# MAX_PATH; the launcher refuses a TEMP whose worst case exceeds this (review s12-launcher-a F3).
BASETEMP_PREFIX_LIMIT = 70
SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A whole checkpoint suite can exceed run_check's default 3600 s (session 7: CP10 was cut off).
CHECK_TIMEOUT = "14400"


class LauncherError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main_checkout(start: Path) -> Path:
    """The main checkout of the repository holding ``start`` (the parent of the common git dir)."""
    out = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise LauncherError(f"not a git checkout: {start}")
    return Path(out.stdout.strip()).parent


def load_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        keys = ", ".join(f"{k} ({v})" for k, v in CONFIG_KEYS.items())
        raise LauncherError(f"launcher configuration missing: {path}; it needs {keys}")
    config = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in CONFIG_KEYS if not isinstance(config.get(key), str)]
    if missing:
        raise LauncherError(f"launcher configuration {path} lacks {', '.join(missing)}")
    unknown = sorted(set(config) - set(CONFIG_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise LauncherError(f"launcher configuration {path} has unknown keys {unknown}")
    return {key: str(value) for key, value in config.items()}


def target_paths(target: Path) -> dict[str, Path]:
    target = target.resolve()
    paths = {
        "root": target,
        "package": target / "src" / "prometheos",
        "venv": target / ".venv",
        "python": target / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else target / ".venv" / "bin" / "python",
        "run_check": target / "tools" / "run_check.py",
    }
    if not paths["package"].is_dir() or not paths["run_check"].is_file():
        raise LauncherError(f"not a PrometheOS checkout: {target}")
    if not paths["python"].is_file():
        raise LauncherError(
            f"{target} has no .venv of its own; create it there with `uv sync --locked` "
            "(and the extras the run needs) before launching"
        )
    return paths


def resolve_run_dir(target: Path, run_dir: str | Path) -> Path:
    """Relative run folders belong to the target (review s10f-a F7: they used to land in the
    main checkout whatever the target was)."""
    path = Path(run_dir)
    return (path if path.is_absolute() else target.resolve() / path).resolve()


def synthetic_root(temp_root: Path) -> Path:
    """A new short folder ``<temp>\\pl<3 chars>``: with the synthetic TEMP inside it, pytest's
    default basetemp from decision 0048 is ``<temp>\\plxxx\\t\\ptb\\<pid>``, a few characters
    shorter than the session-10 launcher's explicit one. Folders of this launcher older than a
    day are removed (only those holding its marker file)."""
    temp_root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - ENV_KEEP_SECONDS
    for old in temp_root.glob("pl???"):
        marker = old / ENV_MARKER
        try:
            if marker.is_file() and marker.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    for _ in range(100):
        root = temp_root / ("pl" + "".join(secrets.choice(alphabet) for _ in range(3)))
        try:
            root.mkdir()
        except FileExistsError:
            continue
        (root / ENV_MARKER).write_text(now() + "\n", encoding="utf-8")
        return root
    raise LauncherError(f"no free synthetic folder under {temp_root}")


def basetemp_prefix(temp: Path) -> int:
    """The worst-case length of a pytest worker's basetemp under ``temp``: a 7-digit pid and a
    two-digit xdist worker."""
    return len(str(temp / "ptb" / ("9" * 7) / "popen-gw99"))


def environment(paths: dict[str, Path], config: dict[str, str], env_root: Path) -> dict[str, str]:
    """The whole environment of a launched check: nothing is inherited except the user name."""
    home = env_root / "h"
    temp = env_root / "t"
    for path in (home, home / "AppData" / "Roaming", home / "AppData" / "Local", temp):
        path.mkdir(parents=True, exist_ok=True)
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    system_root = os.environ.get("SystemRoot", r"C:\WINDOWS")
    return {
        "PROCESSOR_ARCHITECTURE": "AMD64",
        "USERNAME": f"{domain}\\{user}" if domain else user,
        # Without SystemDrive, Windows font-cache writers create a literal "%SystemDrive%"
        # folder in the working directory (seen in the first Chromium e2e run).
        "SystemDrive": system_root[:2],
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": os.pathsep.join(
            [
                str(paths["python"].parent),
                str(Path(config["uv"]).parent),
                config["node_dir"],
                config["git_cmd_dir"],
                str(Path(system_root) / "System32"),
                str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
            ]
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PROMETHEOS_HOME": str(home / "prometheos"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(temp),
        "TMP": str(temp),
        "UV_CACHE_DIR": config["uv_cache_dir"],
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "UV_PROJECT_ENVIRONMENT": str(paths["venv"]),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_AUDIT": "false",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        # LOCALAPPDATA is synthetic, so Playwright is pointed at the owner-approved cache.
        "PLAYWRIGHT_BROWSERS_PATH": config["playwright_browsers_path"],
        "PROMETHEOS_TEX_ENGINE_DIR": config["tectonic_dir"],
        "PROMETHEOS_MODELS_DIR": config["models_dir"],
        "PROMETHEOS_REQUIRE_TECTONIC": "1",
        "PROMETHEOS_REQUIRE_REAL_MODEL": "1",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "NUL" if sys.platform == "win32" else "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }


def git(target: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def tree_digest(target: Path) -> str:
    """The target's own tree digest (its tools/tree_manifest.py, run by its venv)."""
    paths = target_paths(target)
    code = (
        "import sys; sys.path.insert(0, 'tools'); import tree_manifest; "
        "print(tree_manifest.tree_sha256(__import__('pathlib').Path('.').resolve()))"
    )
    out = subprocess.run(
        [str(paths["python"]), "-c", code],
        cwd=paths["root"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise LauncherError(f"tree digest failed in {target}: {out.stderr.strip()[-400:]}")
    return out.stdout.strip().splitlines()[-1]


def imported_package(paths: dict[str, Path], env: dict[str, str]) -> Path:
    """Where the target's interpreter imports ``prometheos`` from under the launch environment."""
    out = subprocess.run(
        [
            str(paths["python"]),
            "-c",
            "import prometheos, pathlib; print(pathlib.Path(prometheos.__file__).resolve().parent)",
        ],
        cwd=paths["root"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise LauncherError(f"prometheos does not import in {paths['root']}: {out.stderr[-400:]}")
    return Path(out.stdout.strip().splitlines()[-1])


def preflight(
    paths: dict[str, Path], config: dict[str, str], env: dict[str, str], extras: list[str]
) -> dict[str, Any]:
    """Refuse to launch unless the target's venv is in sync with its lockfile and imports the
    target's own ``src``."""
    sync = [config["uv"], "sync", "--locked", "--offline", "--check"]
    for extra in extras:
        sync += ["--extra", extra]
    out = subprocess.run(sync, cwd=paths["root"], env=env, capture_output=True, text=True)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()[-600:]
        hint = " ".join(["uv", "sync", "--locked", *[f"--extra {e}" for e in extras]])
        # The check is exact (review s12-launcher-a F7): --extra must name every extra the venv
        # has, so the tested environment is exactly the locked one.
        raise LauncherError(
            f"{paths['root']}: .venv is not exactly the locked environment with extras "
            f"{extras or 'none'} ({detail}); run `{hint}` there, or pass the venv's extras"
        )
    package = imported_package(paths, env)
    if package != paths["package"].resolve():
        raise LauncherError(
            f"{paths['root']}: its venv imports prometheos from {package}, not from its own src"
        )
    return {"uv_sync_check": sync[1:], "extras": extras, "prometheos_imported_from": str(package)}


def launcher_record(
    paths: dict[str, Path],
    env: dict[str, str],
    checks: dict[str, Any],
    command: list[str],
    tree_before: str,
) -> dict[str, Any]:
    root = paths["root"]
    return {
        "schema_version": 1,
        "kind": "prometheos-launcher-record",
        "launcher": {
            "path": str(HERE),
            "sha256": hashlib.sha256(HERE.read_bytes()).hexdigest(),
        },
        "target": {
            "root": str(root),
            "head": git(root, "rev-parse", "HEAD"),
            "branch": git(root, "branch", "--show-current"),
            "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=no")),
            "venv": str(paths["venv"]),
            "tree_sha256_before": tree_before,
        },
        "preflight": checks,
        "command": command,
        "environment": env,
        "started_at": now(),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_command(paths: dict[str, Path], command: list[str]) -> list[str]:
    if command[:1] == ["pytest"]:
        return [str(paths["python"]), "-m", "pytest", *command[1:]]
    if command[:1] == ["python"]:
        return [str(paths["python"]), *command[1:]]
    return command


def recorded(
    paths: dict[str, Path], env: dict[str, str], run_dir: Path, name: str, command: list[str]
) -> int:
    runner = [
        str(paths["python"]),
        str(paths["run_check"]),
        "--run-dir",
        str(run_dir),
        "--name",
        name,
        "--timeout",
        CHECK_TIMEOUT,
        "--root",
        str(paths["root"]),
        "--",
        *command,
    ]
    return subprocess.run(runner, cwd=paths["root"], env=env, check=False).returncode


def prepare(args: argparse.Namespace) -> tuple[dict[str, Path], dict[str, str], dict[str, Any]]:
    paths = target_paths(Path(args.target))
    config_path = (
        Path(args.config)
        if args.config
        else main_checkout(HERE.parent) / "local" / ("integration-launcher.json")
    )
    config = load_config(config_path)
    temp_root = Path(config.get("temp_root") or os.environ.get("TEMP") or tempfile.gettempdir())
    env_root = synthetic_root(temp_root)
    env = environment(paths, config, env_root)
    prefix = basetemp_prefix(Path(env["TEMP"]))
    if prefix > BASETEMP_PREFIX_LIMIT:
        shutil.rmtree(env_root, ignore_errors=True)
        raise LauncherError(
            f"temp_root {temp_root} is too long: worker basetemps would reach {prefix} "
            f"characters (limit {BASETEMP_PREFIX_LIMIT}); choose a shorter temp_root"
        )
    checks = preflight(paths, config, env, list(args.extra or []))
    checks |= {"config": str(config_path), "basetemp_prefix_max": prefix}
    return paths, env, checks


def finish(env: dict[str, str], ok: bool) -> None:
    """Remove the synthetic folder after a passing invocation; keep it after a failure, for
    diagnosis (review s12-launcher-a F8; older ones go after a day)."""
    if ok:
        shutil.rmtree(Path(env["TEMP"]).parent, ignore_errors=True)


def cmd_check(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise LauncherError("missing command after --")
    paths, env, checks = prepare(args)
    run_dir = resolve_run_dir(paths["root"], args.run_dir)
    if run_dir.is_relative_to(paths["root"] / "development"):
        # The launcher record holds host paths and the user name (review s12-launcher-a F6);
        # receipts are written by run_checkpoint into their own folders.
        raise LauncherError("a check's run folder must not be under development/")
    command = check_command(paths, command)
    record = launcher_record(paths, env, checks, command, tree_digest(paths["root"]))
    write_json(run_dir / f"{args.name}.launcher.json", record)
    print(f"target {paths['root']}\nrun-dir {run_dir}\ncommand {command}", flush=True)
    code = recorded(paths, env, run_dir, args.name, command)
    record |= {
        "ended_at": now(),
        "exit_code": code,
        "tree_sha256_after": tree_digest(paths["root"]),
    }
    write_json(run_dir / f"{args.name}.launcher.json", record)
    finish(env, code == 0)
    return code


def log_line(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def cmd_integration(args: argparse.Namespace) -> int:
    if not SUFFIX.fullmatch(args.suffix):
        raise LauncherError(f"suffix {args.suffix!r} must match {SUFFIX.pattern}")
    paths, env, checks = prepare(args)
    root = paths["root"]
    order = (
        [cp.strip() for cp in ",".join(args.only).split(",") if cp.strip()]
        if args.only
        else (list(DEFAULT_ORDER))
    )
    driver = resolve_run_dir(root, args.driver_dir or f"scratch/int/{args.suffix}-driver")
    if driver.is_relative_to(root / "development"):  # review s12-launcher-a R2-2
        raise LauncherError("the driver folder must not be under development/")
    if driver.exists() and any(driver.iterdir()):
        raise LauncherError(f"driver folder {driver} already has content; pick a new suffix")
    log = driver / "driver.log"
    before = tree_digest(root)
    record = launcher_record(paths, env, checks, ["integration", *order], before)
    record["suffix"] = args.suffix
    write_json(driver / "launcher.json", record)
    log_line(log, f"target {root} head {record['target']['head']} tree before: {before}")
    reuse = [r for r in ",".join(args.reuse_from or []).split(",") if r]
    results: dict[str, int] = {}
    screenshots_tracked = bool(git(root, "ls-files", "hub/screenshots"))
    for cp in order:
        run = f"{cp.lower()}-{args.suffix}"
        log_line(log, f"=== {cp} {run} {datetime.now(UTC).strftime('%H:%M:%S')}")
        command = [
            str(paths["python"]),
            "tools/run_checkpoint.py",
            cp,
            run,
            *(["--implementer", args.implementer] if args.implementer else []),
        ]
        if reuse:
            command += ["--reuse-from", *reuse]
        code = recorded(paths, env, driver, f"run-{cp.lower()}", command)
        results[cp] = code
        stdout = driver / f"run-{cp.lower()}.stdout.txt"
        tail = (
            stdout.read_text(encoding="utf-8", errors="replace").splitlines()[-2:]
            if (stdout.is_file())
            else []
        )
        for line in tail:  # run_checkpoint's summary, as run_all.ps1 kept (review F5)
            log_line(log, f"  {line}")
        log_line(log, f"exit {code} {cp}")
        reuse.append(f"development/receipts/{cp}/{run}")  # decision 0037
        if screenshots_tracked:
            # e2e journeys rewrite the tracked screenshots; restore them between stages.
            git(root, "checkout", "-q", "--", "hub/screenshots")
    try:
        after = tree_digest(root)
    except LauncherError as exc:  # the END line is always written (review F5)
        after = f"unavailable ({exc})"
    stable = after == before
    log_line(log, f"tree after: {after}")
    log_line(log, "TREE STABLE" if stable else "TREE CHANGED DURING RUN")
    failed = sorted(cp for cp, code in results.items() if code != 0)
    problems = failed + ([] if stable else ["tree"])
    log_line(log, f"END {'FAILED ' + ','.join(problems) if problems else 'all exit 0'}")
    record |= {
        "ended_at": now(),
        "results": results,
        "tree_sha256_after": after,
        "tree_stable": stable,
    }
    write_json(driver / "launcher.json", record)
    finish(env, stable and not failed)
    return 0 if stable and not failed else 1


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = top.add_subparsers(dest="mode", required=True)
    for mode in ("check", "integration"):
        p = sub.add_parser(mode)
        p.add_argument("--target", required=True, help="the worktree whose code is tested")
        p.add_argument("--config", help="launcher configuration (default: main checkout local/)")
        p.add_argument("--extra", action="append", help="extras the target's venv must have")
        if mode == "check":
            p.add_argument("--run-dir", required=True)
            p.add_argument("--name", required=True)
            p.add_argument("command", nargs=argparse.REMAINDER)
        else:
            p.add_argument("--suffix", required=True)
            p.add_argument("--only", action="append", help="checkpoints, comma separated")
            p.add_argument("--reuse-from", action="append", help="run folders, comma separated")
            p.add_argument("--driver-dir", help="default: <target>/scratch/int/<suffix>-driver")
            p.add_argument("--implementer")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return cmd_check(args) if args.mode == "check" else cmd_integration(args)
    except LauncherError as exc:
        print(f"int_launcher: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
