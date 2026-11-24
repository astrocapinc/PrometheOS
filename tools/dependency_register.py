"""Dependency rights register (spec/09-licensing-sources.md) generated from real metadata.

``--write`` regenerates ``development/dependencies.json`` from ``uv.lock`` and the
installed distributions' own metadata (License-Expression / License / classifiers
and bundled license-file digests). Build-system requirements are installed into
a temporary target directory to read their metadata. Without ``--write`` the
register is checked against the lockfile so a new or changed dependency cannot
enter unregistered.

A metadata license string is a declaration, not legal review: entries carry
``review`` notes and ``unknown`` when no declaration exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from importlib.metadata import Distribution, PackageNotFoundError, PathDistribution, distribution
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "development" / "dependencies.json"
HUB = ROOT / "hub"
# The desktop app (CP19.2, decision 0054): its own npm lock and the pinned Python core it ships.
DESKTOP = ROOT / "desktop" / "app"
CORE_PIN = DESKTOP / "core-pin.json"
NPM_REGISTRY = "https://registry.npmjs.org"


# Facts a metadata licence string cannot carry. They are copied into the register entry and
# surface in the release inventory (E08); nothing here is a legal conclusion.
OPTIONAL_FLAGS: dict[str, list[str]] = {
    "tree-sitter": [
        "Optional extra for pip installs; the desktop installer's core bundles it (decision "
        "0072, dogfood P3: without it the app skipped every MATLAB file it adopted). MIT; its "
        "licence file ships in the installer's THIRD-PARTY-NOTICES.txt.",
    ],
    "tree-sitter-matlab": [
        "Optional extra for pip installs; the desktop installer's core bundles it (decision "
        "0072, dogfood P3: without it the app skipped every MATLAB file it adopted). MIT; its "
        "licence file ships in the installer's THIRD-PARTY-NOTICES.txt.",
    ],
    "av": [
        "Optional audio extra only; never imported by the core. The Windows wheel (av.libs) "
        "bundles FFmpeg 9.x libraries together with libx264 and libx265, which are "
        "GPL-2.0-or-later, although PyAV's own code is BSD-3-Clause. Redistributing the wheel "
        "or an installer that contains it is a GPL question: flagged for E08 "
        "(development/decisions/0013-cp6-routine-decisions.md).",
    ],
    "pyaudiowpatch": [
        "Optional audio extra, Windows only. Wheel bundles PortAudio (MIT) built for WASAPI "
        "loopback. Hardware behaviour is E04 evidence.",
    ],
    "pypdfium2": [
        "Core dependency from CP11 S11.1 (decisions 0060, 0061): page text, character offsets "
        "and quote digests of PDFs. The wheel's own metadata declares 'BSD-3-Clause, Apache-2.0, "
        "dependency licenses'. It bundles a PDFium binary: PDFium itself under its BSD-style "
        "licence (BUILD_LICENSES/pdfium.txt) and the pdfium-binaries build scripts under MIT "
        "(BUILD_LICENSES/pdfium-binaries.txt), built with third-party libraries whose notices "
        "the wheel ships under licenses/data/<platform>/BUILD_LICENSES (abseil, agg23, "
        "fast_float, FreeType, ICU, Little CMS, libjpeg-turbo, OpenJPEG, libpng, libtiff, "
        "llvm-libc, simdutf, zlib), plus CC-BY-4.0 for documentation data. The notice set is "
        "per platform; this register records the windows_x64 one. All must be carried into an "
        "installer's notices file before any public release (E08). PDFium parses untrusted "
        "PDFs in a separate process (src/prometheos/library/extract_worker.py).",
    ],
    "faster-whisper": [
        "Optional audio extra. Model weights are a separate asset (Systran/faster-whisper-base, "
        "MIT) installed by an explicit owner action, never bundled; see "
        "src/prometheos/audio/models.py for the pinned revision and digests.",
    ],
}


def _requirement_names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        head = requirement.split(";")[0]
        names.add(head.split("==")[0].strip().lower())
    return names


def locked_packages() -> dict[str, dict[str, Any]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_direct = _requirement_names(project["project"]["dependencies"])
    extra_direct = {
        extra: _requirement_names(requirements)
        for extra, requirements in project["project"].get("optional-dependencies", {}).items()
    }
    dev_direct = {r.split("==")[0].lower() for r in project["dependency-groups"]["dev"]}
    packages: dict[str, dict[str, Any]] = {}
    # A dependency may ask for extras ({ name = "pyjwt", extra = ["crypto"] }); an extra's own
    # dependencies are in that package's optional-dependencies. Both are followed, so a package
    # reached only through an extra gets the scope of what asks for it (review s3c2-a F3:
    # cryptography, cffi and pycparser reach the runtime through mcp -> pyjwt[crypto]).
    Edge = tuple[str, tuple[str, ...]]
    graph: dict[str, list[Edge]] = {}
    extras_of: dict[str, dict[str, list[Edge]]] = {}

    def edges(entries: list[dict[str, Any]]) -> list[Edge]:
        return [(d["name"].lower(), tuple(d.get("extra", []))) for d in entries]

    for pkg in lock["package"]:
        name = pkg["name"].lower()
        if name == "prometheos":
            continue
        packages[name] = {"version": pkg["version"], "source": pkg.get("source", {})}
        graph[name] = edges(pkg.get("dependencies", []))
        extras_of[name] = {
            extra: edges(entries) for extra, entries in pkg.get("optional-dependencies", {}).items()
        }

    def closure(roots: set[str]) -> set[str]:
        seen: set[str] = set()
        visited: set[Edge] = set()
        stack: list[Edge] = [(root, ()) for root in roots]
        while stack:
            item = stack.pop()
            name, extras = item
            if item in visited or name not in graph:
                continue
            visited.add(item)
            seen.add(name)
            stack.extend(graph[name])
            for extra in extras:
                stack.extend(extras_of[name].get(extra, []))
        return seen

    runtime = closure(runtime_direct)
    optional: dict[str, str] = {}
    for extra, roots in extra_direct.items():
        for name in closure(roots) - runtime:
            optional.setdefault(name, extra)
    for name, info in packages.items():
        if name in runtime:
            info["scope"] = "runtime"
        elif name in optional:
            info["scope"] = f"optional:{optional[name]}"
        else:
            info["scope"] = "development"
        info["direct"] = (
            name in runtime_direct
            or name in dev_direct
            or any(name in roots for roots in extra_direct.values())
        )
    return packages


def license_info(dist: Distribution) -> dict[str, Any]:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    declared = meta.get("License")
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    files = []
    for f in dist.files or []:
        lower = f.name.lower()
        # A PEP 639 licenses folder holds notices under any name (pypdfium2 ships PDFium's
        # third-party notices as licenses/data/<platform>/BUILD_LICENSES/<library>.txt).
        in_licenses = ".dist-info/licenses/" in f.as_posix().lower()
        if in_licenses or any(k in lower for k in ("license", "copying", "notice", "authors")):
            path = Path(str(dist.locate_file(f)))
            if path.is_file():
                files.append(
                    {"file": f.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                )
    return {
        "license_expression": expression or None,
        "declared_license_field": (declared or None) if not expression else None,
        "license_classifiers": classifiers,
        "license_files": sorted(files, key=lambda x: x["file"]),
        "rights_status": (
            "declared_spdx"
            if expression
            else "declared_classifier"
            if classifiers
            else "declared_text"
            if declared
            else "unknown"
        ),
    }


BUILD_CONSTRAINTS = ROOT / "build-constraints.txt"


def build_constraints() -> set[tuple[str, str]]:
    """The pinned build closure (decision 0057), as (name, version)."""
    pins = set()
    for line in BUILD_CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            pins.add((name.strip().lower(), version.strip()))
    return pins


def uv_build_constraints(root: Path = ROOT) -> set[tuple[str, str]]:
    """pyproject's [tool.uv] build-constraint-dependencies (decision 0059), as (name, version)."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pins = project.get("tool", {}).get("uv", {}).get("build-constraint-dependencies", [])
    return {
        (name.strip().lower(), version.strip())
        for name, _, version in (pin.partition("==") for pin in pins)
    }


def build_requirements() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(project["build-system"]["requires"])


def build_entries() -> list[dict[str, Any]]:
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--target",
                str(target),
                "--constraints",
                str(BUILD_CONSTRAINTS),
                *build_requirements(),
            ],
            check=True,
        )
        for dist_info in sorted(target.glob("*.dist-info")):
            dist = PathDistribution(dist_info)
            entries.append(
                {
                    "name": dist.metadata["Name"].lower(),
                    "version": dist.version,
                    "scope": "build",
                    "direct": dist.metadata["Name"].lower()
                    in {r.split("==")[0].lower() for r in build_requirements()},
                    "mode": "build-time tool (not redistributed)",
                    "source": "https://pypi.org/simple",
                    **license_info(dist),
                }
            )
    return entries


def npm_locked(project: Path = HUB) -> dict[tuple[str, str], dict[str, Any]]:
    """npm packages from a project's package-lock.json (the hub, or the desktop app):
    (name, version) -> lock facts."""
    lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
    manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
    direct = set(manifest.get("dependencies", {})) | set(manifest.get("devDependencies", {}))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for path, entry in lock.get("packages", {}).items():
        if not path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        version = entry.get("version", "?")
        found[(name, version)] = {
            "path": path,
            "dev": bool(entry.get("dev")),
            "optional": bool(entry.get("optional")),
            "direct": name in direct and path == f"node_modules/{name}",
        }
    return found


def desktop_scope(name: str, info: dict[str, Any]) -> str:
    """Electron itself is redistributed by the installer (its binaries, with Chromium's
    notices); every other desktop package builds or tests the app and is not shipped."""
    return "desktop_runtime" if info["path"] == "node_modules/electron" else "desktop_development"


def npm_scope(project: Path, name: str, info: dict[str, Any]) -> str:
    if project == DESKTOP:
        return desktop_scope(name, info)
    return "hub_development" if info["dev"] else "hub_runtime"


def npm_entries(project: Path = HUB) -> list[dict[str, Any]]:
    """License facts copied from each installed package's own package.json and files."""
    entries = []
    for (name, version), info in sorted(npm_locked(project).items()):
        folder = project / info["path"]
        scope = npm_scope(project, name, info)
        base: dict[str, Any] = {
            "name": name,
            "version": version,
            "ecosystem": "npm",
            "scope": scope,
            "direct": info["direct"],
            "source": NPM_REGISTRY,
        }
        meta_path = folder / "package.json"
        if not meta_path.is_file():
            entries.append(
                base
                | {
                    "mode": "optional platform package not installed on the recording platform",
                    "license_expression": None,
                    "declared_license_field": None,
                    "license_classifiers": [],
                    "license_files": [],
                    "rights_status": "unverified_not_installed_here",
                }
            )
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        license_value = meta.get("license")
        if isinstance(license_value, dict):
            license_value = license_value.get("type")
        prefix = "" if project == HUB else f"{project.relative_to(ROOT).as_posix()}/"
        files = [
            {
                "file": f"{prefix}{info['path']}/{p.name}",
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in sorted(folder.iterdir())
            if p.is_file() and any(k in p.name.lower() for k in ("license", "licence", "notice"))
        ]
        if name == "electron":
            # Electron's binaries carry their own notices in dist/: the redistributed Chromium's
            # LICENSES.chromium.html and Electron's LICENSE (review int8 CP19-F5, decision 0056).
            dist = folder / "dist"
            files += [
                {
                    "file": f"{prefix}{info['path']}/dist/{p.name}",
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in sorted(dist.iterdir() if dist.is_dir() else [])
                if p.is_file() and "licen" in p.name.lower()
            ]
        if scope in ("hub_development", "desktop_development"):
            mode = "build or test tool (not redistributed)"
        elif scope == "desktop_runtime":
            mode = (
                "redistributed in the desktop installer (Electron's binaries, unmodified; "
                "Chromium's notices in its LICENSES.chromium.html)"
            )
        elif name == "@napi-rs/canvas" or name.startswith("@napi-rs/canvas-"):
            mode = "optional Node-only dependency of pdfjs-dist; never bundled in the web assets"
        else:
            mode = "bundled into the hub web assets (unmodified)"
        entries.append(
            base
            | {
                "mode": mode,
                "license_expression": license_value if isinstance(license_value, str) else None,
                "declared_license_field": None,
                "license_classifiers": [],
                "license_files": files,
                "rights_status": "declared_spdx" if isinstance(license_value, str) else "unknown",
            }
        )
    return entries


def tool_entries() -> list[dict[str, Any]]:
    """External tools PrometheOS installs or fetches on an explicit owner action and never
    redistributes (CP9 A01): the pinned Tectonic engine and its TeX support bundle. Facts
    come from the pin in ``prometheos.app.texengine`` (verified at CP9.1, decision 0012)."""
    from prometheos.app.texengine import TECTONIC

    return [
        {
            "name": TECTONIC.engine,
            "version": TECTONIC.version,
            "ecosystem": "binary",
            "scope": "external_tool",
            "direct": True,
            "mode": "installed by an explicit owner action into the PrometheOS home; "
            "digest-verified; not redistributed",
            "source": TECTONIC.url,
            "sha256": TECTONIC.sha256,
            "license_expression": TECTONIC.license,
            "declared_license_field": None,
            "license_classifiers": [],
            "license_files": [{"file": TECTONIC.license_url, "sha256": None}],
            "rights_status": "declared_spdx",
        },
        {
            "name": "tectonic-default-bundle",
            "version": "v33 (TeX Live 2022)",
            "ecosystem": "data",
            "scope": "external_tool",
            "direct": False,
            "mode": "fetched on demand by the engine into a local cache; not redistributed",
            "source": TECTONIC.bundle_url,
            "sha256": None,
            "license_expression": None,
            "declared_license_field": "TeX Live 2022 material; each package under its own free "
            "licence (per-package terms, no single licence for the bundle)",
            "license_classifiers": [],
            "license_files": [],
            "rights_status": "declared_text",
        },
        core_entry(),
        *installer_entries(),
    ]


def core_entry() -> dict[str, Any]:
    """The Python interpreter the desktop installer ships (CP19.2, decision 0054)."""
    pin = json.loads(CORE_PIN.read_text(encoding="utf-8"))
    return {
        "name": pin["name"],
        "version": pin["version"],
        "ecosystem": "binary",
        "scope": "desktop_runtime",
        "direct": True,
        "mode": "redistributed in the desktop installer (resources/core); " + pin["notes"],
        "source": pin["distribution"],
        "sha256": pin["python_exe_sha256"],
        "license_expression": pin["license"],
        "declared_license_field": None,
        "license_classifiers": [],
        "license_files": [{"file": pin["license_file"], "sha256": pin["license_file_sha256"]}],
        "rights_status": "declared_spdx",
    }


def installer_entries() -> list[dict[str, Any]]:
    """Components electron-builder puts into the desktop installer besides the app (review
    s3c2-a F4): the NSIS stubs and elevate.exe, declared in core-pin.json."""
    pin = json.loads(CORE_PIN.read_text(encoding="utf-8"))
    return [
        {
            "name": item["name"],
            "version": item["version"],
            "ecosystem": "binary",
            "scope": "desktop_runtime",
            "direct": False,
            "mode": f"redistributed in the desktop installer: {item['role']}. {item['notes']}",
            "source": item["source"],
            "sha256": None,
            "license_expression": item["license"],
            "declared_license_field": None,
            "license_classifiers": [],
            "license_files": [],
            "rights_status": "declared_spdx",
        }
        for item in pin["installer_components"]
    ]


def check_tools(packages: list[Any]) -> list[str]:
    """External-tool entries must equal the pins exactly, in both directions: a missing or
    changed pin fails, a stale extra entry fails, and a malformed entry is reported (review
    cp9-a round 2 R2-5) instead of crashing the check."""
    failures: list[str] = []
    recorded: set[tuple[str, str, str | None]] = set()
    for index, entry in enumerate(packages):
        if not isinstance(entry, dict) or entry.get("ecosystem") not in ("binary", "data"):
            continue
        name, version, digest = entry.get("name"), entry.get("version"), entry.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not (digest is None or isinstance(digest, str))
        ):
            failures.append(f"malformed external tool entry at packages[{index}]")
            continue
        recorded.add((name, version, digest))
    expected = {(t["name"], t["version"], t["sha256"]) for t in tool_entries()}
    failures += [
        f"external tool not registered as pinned: {n} {v}"
        for n, v, _ in sorted(expected - recorded)
    ]
    failures += [
        f"registered external tool not pinned in the code: {n} {v}"
        for n, v, _ in sorted(recorded - expected)
    ]
    return failures


def generate() -> dict[str, Any]:
    entries = []
    for name, info in sorted(locked_packages().items()):
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            # Locked for another platform/marker (e.g. emscripten-only); not installed here,
            # so its own metadata cannot be read. Release review must cover it per platform.
            entries.append(
                {
                    "name": name,
                    "version": info["version"],
                    "scope": info["scope"],
                    "direct": info["direct"],
                    "mode": "not installed on the recording platform",
                    "source": info["source"].get("registry", "unknown"),
                    "license_expression": None,
                    "declared_license_field": None,
                    "license_classifiers": [],
                    "license_files": [],
                    "rights_status": "unverified_not_installed_here",
                }
            )
            continue
        if dist.version != info["version"]:
            raise SystemExit(f"installed {name} {dist.version} differs from lock {info['version']}")
        entries.append(
            {
                "name": name,
                "version": info["version"],
                "scope": info["scope"],
                "direct": info["direct"],
                "mode": (
                    "dependency (unmodified)"
                    if info["scope"] == "runtime"
                    else "optional extra dependency (unmodified; not installed by default)"
                    if info["scope"].startswith("optional:")
                    else "development tool (not redistributed)"
                ),
                "source": info["source"].get("registry", "unknown"),
                **license_info(dist),
                **({"flags": OPTIONAL_FLAGS[name]} if name in OPTIONAL_FLAGS else {}),
            }
        )
    entries.extend(build_entries())
    entries.extend(npm_entries())
    entries.extend(npm_entries(DESKTOP))
    entries.extend(tool_entries())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "tools/dependency_register.py",
        "note": (
            "License fields are copied from each distribution's own metadata and bundled "
            "license files. They are declarations recorded at adoption, not legal review. "
            "Release (CP8/C08-RIGHTS) must recheck notices for redistributed components."
        ),
        "copied_or_adapted_code": [],
        "packages": entries,
    }


def check() -> list[str]:
    if not REGISTER.exists():
        return ["development/dependencies.json is missing"]
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    python = [p for p in register["packages"] if p.get("ecosystem", "pypi") == "pypi"]
    tool_failures = check_tools(register["packages"])
    npm = [p for p in register["packages"] if p.get("ecosystem") == "npm"]
    recorded = {(p["name"], p["version"]) for p in python if p["scope"] != "build"}
    locked = {(n, i["version"]) for n, i in locked_packages().items()}
    failures = [f"unregistered locked package {n}=={v}" for n, v in sorted(locked - recorded)]
    failures += tool_failures
    failures += [f"registered package not in lock {n}=={v}" for n, v in sorted(recorded - locked)]
    # The scope is checked too (CP17 session, decision 0053: a package moved from the core to an
    # optional extra still passed while the register said "runtime").
    scopes = {(p["name"], p["version"]): p["scope"] for p in python if p["scope"] != "build"}
    for name, info in sorted(locked_packages().items()):
        recorded_scope = scopes.get((name, info["version"]))
        if recorded_scope is not None and recorded_scope != info["scope"]:
            failures.append(
                f"package {name}=={info['version']} is registered as {recorded_scope}, "
                f"but the lock makes it {info['scope']}"
            )
    build = {(p["name"], p["version"]) for p in python if p["scope"] == "build"}
    # The registered build closure is exactly the pinned one (decision 0057).
    pins = build_constraints()
    # uv's own copy of the pins (decision 0059) must be the same closure.
    uv_pins = uv_build_constraints()
    if uv_pins != pins:
        failures.append(
            "[tool.uv] build-constraint-dependencies differs from build-constraints.txt: "
            f"{sorted(uv_pins ^ pins)}"
        )
    failures += [f"build constraint {n}=={v} not registered" for n, v in sorted(pins - build)]
    failures += [f"registered build package {n}=={v} not pinned" for n, v in sorted(build - pins)]
    for requirement in build_requirements():
        name, _, version = requirement.partition("==")
        if (name.lower(), version) not in build:
            failures.append(f"build requirement {requirement} not registered")
    for p in register["packages"]:
        if p["rights_status"] == "unknown" and (
            p["scope"] in ("runtime", "hub_runtime") or p["scope"].startswith("optional:")
        ):
            failures.append(f"{p['scope']} dependency {p['name']} has unknown license metadata")
    # npm packages, per lock (the hub; the desktop app, decision 0054): every locked package
    # registered with the scope its lock gives it, nothing registered beyond the lock.
    for project, label, scopes_of in (
        (HUB, "hub", ("hub_runtime", "hub_development")),
        (DESKTOP, "desktop", ("desktop_runtime", "desktop_development")),
    ):
        mine = [p for p in npm if p.get("scope") in scopes_of]
        lock = npm_locked(project)
        recorded_keys = {(p["name"], p["version"]) for p in mine}
        failures += [
            f"unregistered {label} package {n}@{v}" for n, v in sorted(set(lock) - recorded_keys)
        ]
        failures += [
            f"registered {label} package not in lock {n}@{v}"
            for n, v in sorted(recorded_keys - set(lock))
        ]
        recorded_scopes = {(p["name"], p["version"]): p["scope"] for p in mine}
        for key, info in sorted(lock.items()):
            expected = npm_scope(project, key[0], info)
            if key in recorded_scopes and recorded_scopes[key] != expected:
                failures.append(
                    f"{label} package {key[0]}@{key[1]} is registered as {recorded_scopes[key]}, "
                    f"but the lock makes it {expected}"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        REGISTER.write_text(
            json.dumps(generate(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    failures = check()
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if not failures:
        print(
            "PASS: dependency register matches uv.lock, build requirements, "
            "hub/package-lock.json and desktop/app/package-lock.json"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
