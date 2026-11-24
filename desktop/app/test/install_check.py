"""The PrometheOS desktop installer, installed, exercised and removed (CP19.2, decision 0054).

Usage (repository root, main environment):
    .venv/Scripts/python.exe desktop/app/test/install_check.py <new out folder>

Steps, recorded in <out>/install.json; exit 0 only when every check holds:
1. this process is not elevated, so the installer cannot use administrator rights;
2. the per-user installer (desktop/app/dist/PrometheOS Setup <version>.exe) runs silently (/S);
   one new per-user uninstall key (HKCU), HKLM's uninstall keys unchanged (a hive that cannot be
   read fails the check), the folder under the user's profile, a Start-menu shortcut carrying
   the app's AppUserModelID, and the updater's installer copy;
3. the installed PrometheOS.exe passes the desktop shell test with its bundled core and no
   Python from this machine (test/shell.spec.ts with PROMETHEOS_DESKTOP_EXE);
4. the uninstaller runs silently; the folder, the HKCU key, the shortcut and the updater copy
   are gone; HKLM is still unchanged; the PrometheOS home the app used (its project store and
   settings) is still there and opens.
Windows only. Synthetic data only.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import winreg
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parents[1]
REPO = APP.parents[1]
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
APP_ID = "org.prometheos.desktop"
NODE = Path(r"C:\Program Files\nodejs\node.exe")


def uninstall_keys(hive: int) -> dict[str, dict[str, str]]:
    """Every uninstall key of a hive, both registry views. A view that exists but cannot be read
    raises: an unreadable hive must not pass as unchanged (review s3b2-a F4)."""
    keys: dict[str, dict[str, str]] = {}
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
        except FileNotFoundError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, name) as key:
                    values = {}
                    for j in range(winreg.QueryInfoKey(key)[1]):
                        value_name, value, _ = winreg.EnumValue(key, j)
                        values[value_name] = str(value)
                keys[name] = values
    return keys


def shortcut_app_id(link: Path) -> str | None:
    script = (
        "$s = (New-Object -ComObject Shell.Application).NameSpace('{}').ParseName('{}'); "
        "$s.ExtendedProperty('System.AppUserModel.ID')"
    ).format(str(link.parent).replace("'", "''"), link.name.replace("'", "''"))
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True)  # fmt: skip
    return out.stdout.strip() or None


def known_folder(name: str) -> Path:
    """A shell folder as Windows resolves it for this process, and so for the installer it
    starts (it may be redirected, for example to OneDrive). Windows expands the profile folders
    with the process environment: under the integration launcher's synthetic USERPROFILE,
    APPDATA and LOCALAPPDATA, the installer and this check both use the synthetic folders.
    DoNotVerify: the folder may not exist yet."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"[Environment]::GetFolderPath('{name}', 'DoNotVerify')"],
                         capture_output=True, text=True, check=True)  # fmt: skip
    folder = out.stdout.strip()
    if not folder:
        raise SystemExit(f"Windows resolves no {name} folder")
    return Path(folder)


def desktop_folder() -> Path:
    return known_folder("Desktop")


def size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    (setup,) = sorted((APP / "dist").glob("PrometheOS Setup *.exe"))
    result: dict[str, Any] = {
        "installer": setup.name,
        "installer_sha256": hashlib.sha256(setup.read_bytes()).hexdigest(),
        "installer_mib": round(setup.stat().st_size / 1048576, 1),
        "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin()),
    }
    if result["elevated"]:
        raise SystemExit("run this as a standard (non-elevated) user")
    user_before = uninstall_keys(winreg.HKEY_CURRENT_USER)
    machine_before = uninstall_keys(winreg.HKEY_LOCAL_MACHINE)
    present = sorted(
        str(v.get("DisplayName"))
        for v in user_before.values()
        if str(v.get("DisplayName", "")).startswith("PrometheOS")
    )
    if present:
        # int11: the per-user installer upgrades an existing installation in place (it reads
        # its folder from the registry, whatever the environment says), so a check run on a
        # computer where PrometheOS is installed would replace the owner's app and could then
        # uninstall it. Refuse before touching anything.
        result["refused"] = (
            f"PrometheOS is already installed for this user ({', '.join(present)}); "
            "uninstall it (the PrometheOS home is kept) and run the check again"
        )
        (out / "install.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(result["refused"], file=sys.stderr)
        return 2
    start_menu = known_folder("Programs")
    local = known_folder("LocalApplicationData")
    updater = local / "prometheos-desktop-updater"
    links_before = set(start_menu.rglob("PrometheOS*.lnk"))
    desktop_link = desktop_folder() / "PrometheOS.lnk"
    desktop_link_before = desktop_link.exists()
    started = time.monotonic()
    done = subprocess.run([str(setup), "/S"], capture_output=True, timeout=900)
    result["install"] = {"exit": done.returncode, "seconds": round(time.monotonic() - started, 1)}
    time.sleep(2)
    user_after = uninstall_keys(winreg.HKEY_CURRENT_USER)
    new = {k: v for k, v in user_after.items() if k not in user_before}
    machine_after = uninstall_keys(winreg.HKEY_LOCAL_MACHINE)
    result["hklm_unchanged_after_install"] = machine_after == machine_before
    if len(new) != 1:
        result["hkcu_new_keys"] = sorted(new)
        for pending in new.values():  # never leave an unexpected install behind (s3c2-a R2-F4)
            quiet = pending.get("QuietUninstallString") or f"{pending['UninstallString']} /S"
            subprocess.run(quiet, capture_output=True, timeout=900)
        (out / "install.json").write_text(json.dumps(result, indent=2), "utf-8")
        return 1
    uninstalled = False
    try:
        ((key_name, values),) = new.items()
        # electron-builder writes no InstallLocation: the uninstaller lives in the install folder.
        uninstall_exe = values["UninstallString"].split('" ')[0].strip('"')
        folder = Path(values.get("InstallLocation") or Path(uninstall_exe).parent)
        if not folder.is_absolute() or not (folder / "PrometheOS.exe").is_file():
            raise SystemExit(f"cannot find the install folder from {values}")  # never a guess
        exe = folder / "PrometheOS.exe"
        local_root = local.resolve()
        links: list[Path] = []
        for _ in range(30):  # the shortcut can appear a moment after the silent installer returns
            links = sorted(set(start_menu.rglob("PrometheOS*.lnk")) - links_before)
            if links:
                break
            time.sleep(1)
        result["installed"] = {
            "display_name": values.get("DisplayName"),
            "folder_under_user_local_app_data": folder.resolve().is_relative_to(local_root),
            "folder": str(folder.resolve()).replace(str(local_root), "%LOCALAPPDATA%"),
            "mib": round(size(folder) / 1048576, 1),
            "bundled_core": (folder / "resources" / "core" / "python.exe").is_file(),
            "start_menu_shortcuts": [p.name for p in links],
            "shortcut_app_ids": [shortcut_app_id(p) for p in links],
            "updater_copy_present": updater.exists(),
            "desktop_shortcut": desktop_link.exists() and not desktop_link_before,
            "core_manifest": json.loads(
                (folder / "resources" / "core" / "MANIFEST.json").read_text("utf-8")
            ),
        }
        keep = out / "kept-home.json"
        test_env = {
            k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))
        }
        test_env |= {"PROMETHEOS_DESKTOP_EXE": str(exe), "PROMETHEOS_DESKTOP_KEEP_HOME": str(keep),
                     "SHELL_PW_OUT": str(out / "playwright")}  # fmt: skip
        shell = subprocess.run(
            [str(NODE), str(APP / "node_modules" / "@playwright" / "test" / "cli.js"), "test",
             "--config", str(APP / "playwright.config.ts")],
            cwd=APP, env=test_env, capture_output=True, text=True, timeout=900,
        )  # fmt: skip
        (out / "shell-test.log").write_text(shell.stdout + shell.stderr, "utf-8")
        result["installed_shell_test"] = {
            "exit": shell.returncode,
            "summary": shell.stdout.strip().splitlines()[-1:],
        }

        uninstaller = values.get("QuietUninstallString") or f"{values['UninstallString']} /S"
        removed = subprocess.run(uninstaller, capture_output=True, timeout=900)
        uninstalled = True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and (
            folder.exists() or key_name in uninstall_keys(winreg.HKEY_CURRENT_USER)
        ):
            time.sleep(1)  # NSIS uninstallers copy themselves to %TEMP% and return early
        kept: dict[str, Any] = json.loads(keep.read_text("utf-8")) if keep.exists() else {}
        home = Path(kept.get("home", ""))
        store_ok = False
        if kept:
            stores = list((home / "projects").glob("project_*/record.sqlite"))
            if stores:
                with sqlite3.connect(f"file:{stores[0].as_posix()}?mode=ro", uri=True) as conn:
                    store_ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        result["uninstall"] = {
            "exit": removed.returncode,
            "folder_removed": not folder.exists(),
            "hkcu_key_removed": key_name not in uninstall_keys(winreg.HKEY_CURRENT_USER),
            "shortcuts_removed": not (set(start_menu.rglob("PrometheOS*.lnk")) - links_before)
            and (desktop_link_before or not desktop_link.exists()),
            "updater_copy_removed": not updater.exists(),
            "hklm_unchanged": uninstall_keys(winreg.HKEY_LOCAL_MACHINE) == machine_before,
            "home_kept": bool(kept) and (home / "desktop" / "settings.json").is_file(),
            "project_store_kept_and_sound": store_ok,
        }
    finally:
        if not uninstalled:
            # A step failed after the install: remove the app anyway (review s3c2-a F8).
            for pending in new.values():
                quiet = pending.get("QuietUninstallString") or f"{pending['UninstallString']} /S"
                subprocess.run(quiet, capture_output=True, timeout=900)
    result["all_ok"] = bool(
        result["install"]["exit"] == 0
        and result["hklm_unchanged_after_install"]
        and result["installed"]["folder_under_user_local_app_data"]
        and result["installed"]["bundled_core"]
        and result["installed"]["shortcut_app_ids"] == [APP_ID]
        and result["installed"]["desktop_shortcut"]
        and result["installed_shell_test"]["exit"] == 0
        and all(result["uninstall"][k] for k in result["uninstall"] if k != "exit")
    )
    (out / "install.json").write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
