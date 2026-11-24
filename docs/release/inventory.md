# Release inventory (for E08)

**Prepared 2026-10-18 for the owner's E08 decision. Nothing is published.** E08, "Owner records licensing/publication decision after concrete release inventory", is recorded by the owner only. This page lists what a public release would contain, under which licences, what stays private and what is still open. Licence fields are the components' own declarations as recorded in the dependency register (`development/dependencies.json`); they are not legal advice.

## 1. The licence decision

- **PrometheOS:** Apache-2.0 for the whole implementation (the owner's choice, 2026-10-18; spec/09 since spec 1.5.2, decision 0072). Root files: `LICENSE` (the Apache-2.0 terms, verbatim), `NOTICE`, `THIRD_PARTY_NOTICES.md`.
- **SPDX fields:** `pyproject.toml` (`license = "Apache-2.0"`, with `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` as licence files in the wheel), `hub/package.json`, `desktop/app/package.json`, and the five bundled skills (`license: Apache-2.0`).
- **Copyright line:** "Copyright 2026 Luis Sebastian Castro Chapa" in `NOTICE` (the owner's choice, 2026-10-19).
- **Contributions:** Developer Certificate of Origin 1.1 (`CONTRIBUTING.md`, spec/09). No CLA.
- **Ownership:** the register records no copied or adapted third-party code (`copied_or_adapted_code: []`). The implementation was written in this repository by the owner with AI coding agents. The earlier MIT-licensed prototype repository was not used as a source (spec/09). Whether any employer or institution policy claims rights in the work is a question only the owner can answer before publishing.

## 2. What ships

| Artifact | How it is built | What it contains | Its notices |
|---|---|---|---|
| Python wheel `prometheos-0.1.0.dev0-py3-none-any.whl` | `uv build --wheel` with the pinned build closure (`build-constraints.txt`) | `src/prometheos`, including the built hub assets (`hub/static/`) and the five skills | `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` in `.dist-info/licenses/` |
| Hub web assets | `tools/build_hub.py` (Vite) from `hub/` | React, CodeMirror 6 and PDF.js bundles (section 3.1) | full texts in `THIRD_PARTY_NOTICES.md` |
| Windows installer `PrometheOS Setup 0.1.0.exe` | `desktop/app/build_core.py`, then `desktop/app/build_app.py --release` | Electron 44.5.1, CPython 3.14.2 with the runtime dependencies and the adoption-parsers extra, the wheel, the NSIS stubs and elevate.exe | `resources/LICENSE`, `resources/NOTICE`, `resources/THIRD-PARTY-NOTICES.txt` (generated from the shipped files), `LICENSE.electron.txt` and `LICENSES.chromium.html` next to `PrometheOS.exe` |
| Agent package | written by `prometheos host setup` on the user's machine | PrometheOS's own MCP entry, skills and hook | Apache-2.0 (original content) |
| Source repository | `tools/public_export.py` | section 5 | `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` |

A release build also fails on any privacy finding in what it ships (the owner's denylist anywhere under `resources/`, every rule over PrometheOS's own files there), and its core carries no bytecode or `direct_url.json`, which would record the build machine's paths.

The release build differs from the test build in two ways, both read back from the built app after each build (`desktop/app/test/release_check.js`): Electron's `enableNodeCliInspectArguments` fuse is off, and the packaged `package.json` carries `prometheosRelease`, which makes the shell ignore `PROMETHEOS_DESKTOP_TEST`, so its test hooks (`prometheosShell` with `state`, `poll`, `clickTray` and `menu`) are never exposed. The test build, which the shell test, the installer check and C19-WALK drive, keeps the fuse, because Playwright attaches through the inspector.

## 3. Bundled third-party components

### 3.1 In the wheel and the installer: the hub's web bundle

18 npm packages, all permissive: MIT (CodeMirror 6, Lezer, React, React DOM, scheduler, style-mod, crelt, w3c-keyname, find-cluster-break) and Apache-2.0 (pdfjs-dist). **Compatible.** pdfjs-dist's optional Node-only `@napi-rs/canvas` is installed but never bundled; its platform variants are "unverified, not installed here" in the register and do not ship.

### 3.2 In the installer: the Python runtime

Every runtime dependency in `uv.lock` (section 2 of `THIRD_PARTY_NOTICES.md`): MIT, BSD-2/3-Clause, ISC, Apache-2.0, MIT-0, PSF-2.0 (typing-extensions, pywin32), "Apache-2.0 OR BSD" (cryptography, packaging). **Compatible.** Points to note:

- **pypdfium2 5.13.0 and PDFium (core dependency).** Declared "BSD-3-Clause, Apache-2.0, dependency licenses". The wheel ships PDFium's licence and the notices of the libraries PDFium is built with (abseil, AGG 2.3, fast_float, FreeType, ICU, Little CMS, libjpeg-turbo, OpenJPEG, libpng, libtiff, llvm-libc, simdutf, zlib), plus CC-BY-4.0 for documentation data. All permissive; FreeType is used under its FreeType Licence (not the GPL option), whose credit line is in `NOTICE` (to confirm: FTL's credit names a year, which the build does not state). **Compatible; notices carried** into `THIRD-PARTY-NOTICES.txt` from the wheel's own files. The notice set is per platform; the register records `windows_x64`.
- **tree-sitter 0.26.0 and tree-sitter-matlab 1.3.1 (adoption-parsers extra).** MIT (decision 0053). Now bundled in the installer's core, so the desktop app parses MATLAB files when it adopts a folder (the real-use finding P3). **Compatible.**
- **httpx2-jsfetch 1.0.** Locked for Emscripten only; not installed on Windows or Linux and not shipped. Its licence was never read (rights "unverified").

### 3.3 In the installer: the desktop runtime

- **Electron 44.5.1** (MIT) with Chromium's notices in `LICENSES.chromium.html`. Chromium's bundle includes LGPL-2.1 components such as `ffmpeg.dll`, shipped unmodified as dynamic libraries. **Unclear, to confirm:** whether a pointer to their source (Chromium's and Electron's published sources) should be added to the notices.
- **Microsoft redistributables** inside Electron (`d3dcompiler_47.dll`, `dxil.dll`; `dxcompiler.dll` is under the LLVM/NCSA licence) and CPython (`vcruntime140.dll`, `vcruntime140_1.dll`). Proprietary but redistributable by design. **Unclear, to confirm:** whether their redistribution terms need a notice.
- **CPython 3.14.2** (PSF-2.0, python-build-standalone). `LICENSE.txt` covers CPython, bzip2, Zstandard and Tcl/Tk (Tcl/Tk is left out of the core). It does not carry the texts of OpenSSL 3 (Apache-2.0), libffi (MIT), SQLite (public domain), zlib, liblzma (0BSD), mpdecimal (BSD-2-Clause), Expat (MIT) and HACL* (MIT or Apache-2.0), which the build also ships. All permissive. **Open:** their verbatim upstream texts are not on disk; `THIRD-PARTY-NOTICES.txt` lists them as an open item rather than reconstructing them.
- **NSIS 3.0.4.1** installer and uninstaller stubs: zlib/libpng licence, with the LZMA module under CPL-1.0 (weak copyleft) and a linking exception. Shipped unmodified; `COPYING` is in the notices. **Compatible as shipped; to confirm:** whether CPL-1.0's object-code clause wants a pointer to NSIS's source (nsis.sourceforge.io).
- **elevate.exe** from electron-builder 26.17.0 (MIT). Shipped but unused (`allowElevation` is false).

### 3.4 Not shipped: optional extras, external tools and development tools

- **The audio extra** (faster-whisper, ctranslate2, onnxruntime, PyAV, PyAudioWPatch and their dependencies). Installed only by a user's explicit choice. **Flag: copyleft.** PyAV's Windows wheel bundles FFmpeg with libx264 and libx265, which are GPL-2.0-or-later (decision 0013). It must never be bundled into a release artifact unless that is resolved. certifi and tqdm (MPL-2.0) come only with this extra.
- **Speech model weights** (faster-whisper-base, MIT): downloaded by an explicit owner action, never bundled.
- **Tectonic 0.17.0** (MIT) and its TeX Live 2022 bundle (each package under its own free licence): installed or fetched on an explicit action, digest-verified, never redistributed.
- **Zotero** (AGPL-3.0): the user's own installation, reached over its local HTTP API; never bundled or linked.
- **Development tools**: 11 Python, 70 hub and 270 desktop packages, build or test only. They include MPL-2.0 tools (axe-core, lightningcss), none of which ships.

### 3.5 Original assets

The desktop icon (`desktop/app/assets/icon.*`) was generated by the project's own script (`desktop/spike/make_icon.py`). The hub uses system fonts and ships no font or image files. Fixtures are synthetic.

## 4. What stays private

The private repository keeps everything below; none of it enters the public export:

- `development/`: state, receipts, evidence, decision records, impact records, reviews, prompts, the HANDOFF and the real-use findings. Only `development/dependencies.json` (the rights register) is exported, because the notices and the register check read it.
- `dogfood/`, `local/` (toolchains, the privacy denylist), `scratch/`, build outputs and every application store, recording or credential (all git-ignored).
- `desktop/spike/` (the CP19.1 comparison prototypes), `hub/screenshots/` (taken during development; the hub UI/UX pass supplies public ones) and `.claude/`.
- The owner's paths, name, e-mail address, institution and research. The export's commit uses a placeholder identity.

Four sealed links in `spec/00-decisions.md` point at decision records 0008, 0010, 0011 and 0032, so they do not resolve in the public tree; the package verifier counts them there instead of failing. Those four records pass the privacy scan, but they quote the owner's answers and name the owner's research area. Publishing them is the owner's choice.

## 5. The public export and its checks

`uv run python tools/public_export.py --out <folder outside the repository> --names-from dogfood` copies the tracked files under an allow-list (README and community files, licence files, `pyproject.toml`, `uv.lock`, `build-constraints.txt`, the specification package, `src/`, `hub/`, `desktop/app/`, `docs/`, `contracts/`, `schemas/`, `fixtures/`, `templates/`, `tests/`, `tools/`, `.github/` and the register), writes `PUBLIC-EXPORT.json`, runs the privacy scan and, only when it is clean, commits the tree as a new one-commit repository on `main`. It never adds a remote or pushes.

`tools/privacy_scan.py` fails on user-home paths (other than generic examples), e-mail addresses (outside example domains and licence texts), private keys and GitHub, Anthropic, OpenAI, AWS, Slack and Google tokens and JSON Web Tokens, the terms in the git-ignored owner denylist, and the distinctive file and folder names harvested from `dogfood/`.

## 6. Release to-dos

| Item | State |
|---|---|
| Electron inspector fuse off in the release build (decision 0054 §8) | **Done**: `build_app.py --release`, read back after the build |
| Shell test hooks out of the release build (decision 0055 §2a) | **Done**: the release stamp disables them; the hook code remains in `main.js`, inert |
| Installer notices file (decision 0054 §8–§9) | **Done, with open items**: generated from the shipped files; open are the CPython-bundled library texts, the Microsoft runtime terms and the source pointers for NSIS (CPL-1.0) and Chromium's LGPL components |
| Locked build requirements (decision 0054 §8) | **Done** before this pass: `build-constraints.txt` and `[tool.uv] build-constraint-dependencies` (decisions 0057, 0059); the desktop's npm lock and the project-local electron-builder cache |
| Adoption parsers in the desktop core (P3) | **Done**: `build_core.py` installs the extra |
| Authenticode signing (decision 0050 §3) | **Owner decision.** Until a certificate signs the installer, SmartScreen warns; electron-builder signs automatically once one is configured |
| The shell's 2,000-event resume path has no test (s3c2-a F7) | Open; not a release blocker |
| Owner identity for the public commit, and the copyright line in `NOTICE` | **Owner**: `git commit --amend --reset-author` (with `-s` for a sign-off of their own) |
| GitHub settings: private vulnerability reporting on (SECURITY.md relies on it), the repository URL in README's clone command | **Owner**, after creating the repository |
| SECURITY.md's "acknowledgement within one week" | **Owner** to confirm or change |
| First CI run | Never run: Actions cannot run before the push. The YAML is validated statically. Every one of its commands was run on Windows 11 in a clean clone of the export: all passed except the parallel test step, where 2 of 2,618 tests failed (an intermittent `PermissionError` on an atomic rename in a CP10 authority test, and an intermittent live-recording race in the CP10 parity test; run alone, the pair passed two runs of three). The Ubuntu jobs have never run, and the Ubuntu Python job reports without gating until it is green |
| Screenshots in README | From the hub UI/UX pass |
| PyPI | Not planned; the `Private :: Do Not Upload` classifier stays until the owner decides |
| Release installer upload | **Owner**, only after the installer notices' open items above are resolved: build with `--release` (it fails on any privacy finding in what it ships), run the installer check on the test build, attach the installer to a GitHub release. A source-only first release needs none of this |
