# PrometheOS desktop application (CP19.2, decision 0054)

An Electron window on the loopback hub. The PrometheOS core runs as a sidecar (`python -m prometheos hub serve --sidecar`) that the app starts and stops. It installs per user, with no administrator rights.

## Build the installer

Run from the repository root.

1. Install the pinned CPython 3.14.2 once, into the git-ignored `local/toolchains/python`. `--no-bin` keeps uv from adding anything to PATH:

   ```
   set UV_PYTHON_INSTALL_DIR=local\toolchains\python
   uv python install cpython-3.14.2-windows-x86_64-none --no-bin
   ```

2. Build the core folder:

   ```
   .venv\Scripts\python.exe desktop\app\build_core.py
   ```

   It writes `desktop/app/build/core`: CPython (checked against `core-pin.json`), the locked runtime dependencies and the prometheos wheel, plus `MANIFEST.json`.

3. Build the per-user installer:

   ```
   cd desktop\app && npm ci && npm run dist
   ```

   The result is `dist\PrometheOS Setup <version>.exe`: electron-builder NSIS, one-click, per user, installed under `%LOCALAPPDATA%\Programs\prometheos-desktop`.

## Run and test

- Development: `cd desktop\app && npx electron . --project <id>`, with `PROMETHEOS_PYTHON` pointing at a Python that has PrometheOS (for example the repository's `.venv`).
- Shell test: `cd desktop\app && npx playwright test`. It drives the real app on a fresh synthetic home: the development Electron by default, or an installed or unpacked `PrometheOS.exe` named by `PROMETHEOS_DESKTOP_EXE`.
- Installer test: `.venv\Scripts\python.exe desktop\app\test\install_check.py <new out folder>`. It runs these steps:
  1. installs silently as this (non-elevated) user;
  2. checks HKCU, HKLM, the folder, the Start-menu shortcut and its AppUserModelID;
  3. runs the shell test against the installed app with its bundled core;
  4. uninstalls;
  5. checks that the updater copy is gone and the PrometheOS home is kept.

## Where things live

- Settings (window bounds, the project, the hotkey, announced events) are in `<PrometheOS home>\desktop\settings.json`. The Electron profile is in `<PrometheOS home>\desktop\electron`.
- The home defaults to `%LOCALAPPDATA%\PrometheOS-v1`, or `PROMETHEOS_HOME`. Uninstalling the app never touches it.
