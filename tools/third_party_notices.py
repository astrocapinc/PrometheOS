"""Third-party notices for what PrometheOS ships (decision 0072), generated from the register.

    uv run python tools/third_party_notices.py            # write THIRD_PARTY_NOTICES.md
    uv run python tools/third_party_notices.py --check    # fail when it is stale
    uv run python tools/third_party_notices.py --installer OUT [--core desktop/app/build/core]

``THIRD_PARTY_NOTICES.md`` lists every redistributed component by artifact, from
``development/dependencies.json``, and carries the full licence texts of the packages bundled
into the hub's web assets, which the wheel ships. Each text is read from the installed file
the register names, and its SHA-256 must equal the register's, so the notice is the file the
register reviewed.

``--installer`` writes the desktop installer's full-text notices file from what the built
core actually contains: every licence file of every ``*.dist-info`` in its site-packages
(each of which must be a registered runtime or adoption-parsers package at that version),
CPython's ``LICENSE.txt``, the hub texts, Electron's licence, NSIS's ``COPYING`` and
electron-builder's licence (for elevate.exe). What no shipped file carries is listed as an
open item, never filled in from memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "development" / "dependencies.json"
OUT = ROOT / "THIRD_PARTY_NOTICES.md"
HUB = ROOT / "hub"
DESKTOP = ROOT / "desktop" / "app"
CORE = DESKTOP / "build" / "core"
NSIS_COPYING = (
    ROOT / "local" / "toolchains" / "electron-builder-cache" / "nsis" / "nsis-3.0.4.1" / "COPYING"
)
# The desktop core installs the runtime dependencies plus this extra (decision 0072).
CORE_EXTRAS = ("adoption-parsers",)
# Bundled into the hub's web assets: hub_runtime minus pdfjs-dist's optional Node-only canvas
# (hub/LICENSES.md: the browser build maps `canvas` to false).
NOT_BUNDLED_PREFIX = "@napi-rs/canvas"

# Libraries inside the python-build-standalone CPython whose notices its LICENSE.txt does not
# carry (desktop/app/core-pin.json). No shipped file holds their texts, so they stay open.
CPYTHON_UNCOVERED = [
    ("OpenSSL 3 (libcrypto-3-x64.dll, libssl-3-x64.dll)", "Apache-2.0"),
    ("libffi (libffi-8.dll)", "MIT"),
    ("SQLite (sqlite3.dll)", "blessing (public domain)"),
    ("zlib (zlib1.dll)", "Zlib"),
    ("liblzma (in _lzma.pyd)", "0BSD"),
    ("mpdecimal (in _decimal.pyd)", "BSD-2-Clause"),
    ("Expat (in pyexpat.pyd)", "MIT"),
    ("HACL* (in the hashing modules)", "MIT OR Apache-2.0"),
    (
        "Microsoft Visual C++ runtime (vcruntime140.dll, vcruntime140_1.dll)",
        "Microsoft redistributable terms",
    ),
]


class NoticeError(Exception):
    pass


def load_register() -> list[dict[str, Any]]:
    return list(json.loads(REGISTER.read_text(encoding="utf-8"))["packages"])


def licence_of(entry: dict[str, Any]) -> str:
    if entry.get("license_expression"):
        return str(entry["license_expression"])
    if entry.get("declared_license_field"):
        return str(entry["declared_license_field"])
    classifiers = entry.get("license_classifiers") or []
    if classifiers:
        return "; ".join(c.split(" :: ")[-1] for c in classifiers)
    return "unknown"


def hub_bundled(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            p
            for p in packages
            if p.get("scope") == "hub_runtime" and not p["name"].startswith(NOT_BUNDLED_PREFIX)
        ),
        key=lambda p: p["name"],
    )


def by_scope(packages: list[dict[str, Any]], *scopes: str) -> list[dict[str, Any]]:
    return sorted((p for p in packages if p.get("scope") in scopes), key=lambda p: p["name"])


def read_verified(path: Path, digest: str | None, label: str) -> str:
    if not path.is_file():
        raise NoticeError(f"{label}: {path.relative_to(ROOT).as_posix()} is missing (npm ci?)")
    data = path.read_bytes()
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise NoticeError(f"{label}: {path.name} differs from the register's digest")
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()


def hub_texts(packages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    texts = []
    for entry in hub_bundled(packages):
        label = f"{entry['name']} {entry['version']}"
        files = entry.get("license_files") or []
        if not files:
            raise NoticeError(f"{label} has no licence file in the register")
        body = "\n\n".join(read_verified(HUB / f["file"], f["sha256"], label) for f in files)
        texts.append((f"{label} ({licence_of(entry)})", body))
    return texts


def table(rows: list[dict[str, Any]], note: Any = None) -> list[str]:
    out = ["| Component | Version | Licence |", "|---|---|---|"]
    for p in rows:
        extra = f" ({note(p)})" if note and note(p) else ""
        out.append(f"| {p['name']} | {p['version']} | {licence_of(p)}{extra} |")
    return out


def platform_note(p: dict[str, Any]) -> str:
    if p.get("rights_status") == "unverified_not_installed_here":
        return "not installed on Windows; its own metadata was not read"
    return ""


def flag_note(p: dict[str, Any]) -> str:
    """A flag the register carries beyond the declared licence (review F8: PyAV's wheel)."""
    flags = " ".join(p.get("flags") or [])
    if "GPL" in flags:
        return "its Windows wheel also bundles GPL code; see the release inventory"
    return platform_note(p)


def render(packages: list[dict[str, Any]]) -> str:
    runtime = by_scope(packages, "runtime")
    parsers = by_scope(packages, *(f"optional:{e}" for e in CORE_EXTRAS))
    desktop = by_scope(packages, "desktop_runtime")
    optional = sorted(
        (
            p
            for p in packages
            if str(p.get("scope", "")).startswith("optional:")
            and p.get("scope") not in {f"optional:{e}" for e in CORE_EXTRAS}
        ),
        key=lambda p: p["name"],
    )
    external = by_scope(packages, "external_tool")
    lines = [
        "# Third-party notices",
        "",
        "Generated by `tools/third_party_notices.py` from the dependency register; do not edit "
        "by hand. PrometheOS itself is under the Apache License 2.0 ([LICENSE](LICENSE), "
        "[NOTICE](NOTICE)). Licence fields are the components' own declarations, recorded "
        "when each was adopted; they are not legal advice. The release inventory "
        "([docs/release/inventory.md](docs/release/inventory.md)) discusses compatibility "
        "and open items.",
        "",
        "## What each artifact ships",
        "",
        "- **The Python wheel** (`prometheos-*.whl`) bundles the hub's web assets, built from "
        "the packages in section 1, whose licence texts are at the end of this file. Its "
        "Python dependencies (section 2) are installed separately by the installer you use, "
        "each with its own licence files.",
        "- **The Windows desktop installer** ships sections 1 to 4: the hub assets, CPython "
        "with the runtime dependencies and the MATLAB parsers, Electron and the NSIS "
        "installer stubs. Its full licence texts are in `resources/THIRD-PARTY-NOTICES.txt`, "
        "with Electron's and Chromium's next to `PrometheOS.exe`.",
        "- **The agent packages** that `prometheos host setup` writes contain only "
        "PrometheOS's own files (Apache-2.0).",
        "",
        "## 1. Bundled into the hub's web assets (wheel and installer)",
        "",
        *table(hub_bundled(packages)),
        "",
        "## 2. Python runtime dependencies (installed with the wheel; bundled in the installer)",
        "",
        *table(runtime, platform_note),
        "",
        "## 3. MATLAB parsing for folder adoption (the `adoption-parsers` extra; bundled in "
        "the installer)",
        "",
        *table(parsers),
        "",
        "## 4. Desktop installer runtime",
        "",
        *table(desktop),
        "",
        "CPython's `LICENSE.txt` does not cover every library the CPython build bundles. These "
        "are shipped too, and their full texts are an open release item "
        "(docs/release/inventory.md):",
        "",
        *(f"- {name}: {spdx}" for name, spdx in CPYTHON_UNCOVERED),
        "",
        "## 5. Not shipped: optional extras and external tools",
        "",
        "Installed only by an explicit choice of the user, never bundled in a release artifact.",
        "",
        *table(optional + external, flag_note),
        "",
        "## Licence texts of the hub's bundled packages",
        "",
    ]
    for title, body in hub_texts(packages):
        lines += [f"### {title}", "", "```text", body, "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def dist_infos(site: Path) -> list[Path]:
    return sorted(site.glob("*.dist-info"))


def dist_licence_files(dist_info: Path) -> list[Path]:
    found = []
    for path in sorted(dist_info.rglob("*")):
        rel = path.relative_to(dist_info).as_posix().lower()
        if path.is_file() and (
            rel.startswith("licenses/")
            or any(k in path.name.lower() for k in ("license", "licence", "copying", "notice"))
        ):
            found.append(path)
    return found


def dist_name_version(dist_info: Path) -> tuple[str, str]:
    name = version = ""
    for line in (dist_info / "METADATA").read_text(encoding="utf-8").splitlines():
        if line.startswith("Name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and not version:
            version = line.split(":", 1)[1].strip()
        elif not line:
            break
    return name, version


def normal(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def installer_text(packages: list[dict[str, Any]], core: Path) -> str:
    site = core / "Lib" / "site-packages"
    if not site.is_dir():
        raise NoticeError(f"no built core at {core}: run desktop/app/build_core.py first")
    shippable = {
        (normal(p["name"]), p["version"])
        for p in packages
        if p.get("scope") == "runtime" or p.get("scope") in {f"optional:{e}" for e in CORE_EXTRAS}
    }
    sections: list[tuple[str, str]] = []
    for dist_info in dist_infos(site):
        name, version = dist_name_version(dist_info)
        if normal(name) == "prometheos":
            continue
        if (normal(name), version) not in shippable:
            raise NoticeError(f"the core ships {name} {version}, which the register does not")
        files = dist_licence_files(dist_info)
        if not files:
            raise NoticeError(f"{name} {version} ships no licence file in its dist-info")
        body = "\n\n".join(
            f"--- {f.relative_to(dist_info).as_posix()} ---\n"
            + f.read_text(encoding="utf-8", errors="replace").strip()
            for f in files
        )
        sections.append((f"{name} {version} (Python package in resources/core)", body))
    cpython = core / "LICENSE.txt"
    if not cpython.is_file():
        raise NoticeError("the core has no CPython LICENSE.txt")
    sections.append(("CPython 3.14.2 and the libraries its LICENSE.txt covers", cpython.read_text(
        encoding="utf-8", errors="replace").strip()))  # fmt: skip
    sections += [(f"{t} (hub web assets in the app)", b) for t, b in hub_texts(packages)]
    electron = DESKTOP / "node_modules" / "electron" / "dist" / "LICENSE"
    sections.append((
        "Electron 44.5.1 (see also LICENSES.chromium.html next to PrometheOS.exe)",
        read_verified(electron, None, "electron"),
    ))  # fmt: skip
    if not NSIS_COPYING.is_file():
        raise NoticeError(f"missing {NSIS_COPYING.relative_to(ROOT).as_posix()} (seed the cache)")
    sections.append(("NSIS 3.0.4.1 (installer and uninstaller stubs)", read_verified(
        NSIS_COPYING, None, "nsis")))  # fmt: skip
    builder = DESKTOP / "node_modules" / "electron-builder" / "LICENSE"
    sections.append(("elevate.exe, from electron-builder 26.17.0", read_verified(
        builder, None, "electron-builder")))  # fmt: skip
    open_items = "\n".join(f"- {n}: {s}" for n, s in CPYTHON_UNCOVERED)
    sections.append((
        "OPEN: libraries bundled by the CPython build whose texts no shipped file carries",
        "These are shipped in resources/core. Their verbatim upstream notices are a release "
        "item still open (docs/release/inventory.md):\n" + open_items,
    ))  # fmt: skip
    head = (
        "PrometheOS desktop application: third-party notices\n\n"
        "PrometheOS is licensed under the Apache License 2.0 (resources/LICENSE). This file "
        "holds the licence texts of the third-party software this installation contains, "
        "generated by tools/third_party_notices.py from the files that were shipped.\n"
    )
    rule = "=" * 78
    parts = [head]
    for title, body in sections:
        parts.append(f"{rule}\n{title}\n{rule}\n\n{body}\n")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--core", type=Path, default=CORE)
    args = parser.parse_args(argv)
    packages = load_register()
    try:
        if args.installer:
            args.installer.parent.mkdir(parents=True, exist_ok=True)
            args.installer.write_text(
                installer_text(packages, args.core), encoding="utf-8", newline="\n"
            )
            print(f"wrote {args.installer}")
            return 0
        text = render(packages)
    except NoticeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print("FAIL: THIRD_PARTY_NOTICES.md is stale; rerun tools/third_party_notices.py",
                  file=sys.stderr)  # fmt: skip
            return 1
        print("PASS: THIRD_PARTY_NOTICES.md matches the register")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
