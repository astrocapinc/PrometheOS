"""Synthetic skill-package builders for CP4 tests (no network, temporary folders only)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from cp1_support import OWNER_ACT

UNSET: Any = object()


def skill_md(name: str, body: str = "Use PrometheOS to record evidence.") -> str:
    return f"---\nname: {name}\ndescription: Synthetic test skill {name}.\n---\n\n{body}\n"


RECIPE: dict[str, Any] = {
    "id": "evidence-pass",
    "title": "Collect and check one piece of evidence",
    "inputs": {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string", "minLength": 3}},
        "additionalProperties": False,
    },
    "steps": [
        {
            "id": "gather",
            "instruction": "Import the relevant source with source_import_plan/commit.",
            "requires": [],
            "expected_artifacts": ["source version"],
            "checks": ["source bytes retained"],
        },
        {
            "id": "optional-plot",
            "instruction": "Optionally plot the data.",
            "requires": [],
            "expected_artifacts": [],
            "checks": [],
            "skippable": True,
        },
        {
            "id": "record",
            "instruction": "Write an entry citing the version with an exact locator.",
            "requires": ["gather"],
            "expected_artifacts": ["entry"],
            "checks": ["entry cites the version", "numbers are bound"],
        },
    ],
    "outputs": {
        "type": "object",
        "required": ["entry_id"],
        "properties": {"entry_id": {"type": "string"}},
    },
}


def write_package(
    folder: Path,
    name: str = "synthetic-evidence",
    version: str = "1.0.0",
    files: dict[str, str | bytes] | None = None,
    licenses: dict[str, str | None] | None = None,
    package_license: str | None = "MIT",
    capabilities: list[str] | None = None,
    recipes: list[dict[str, Any]] | None = None,
    extra_sidecar: dict[str, Any] | None = None,
    skip_components: tuple[str, ...] = (),
) -> Path:
    """Write a package folder with a sidecar declaring every file (unless skipped)."""
    skill = name
    files = (
        files
        if files is not None
        else {
            f"skills/{skill}/SKILL.md": skill_md(skill),
            f"skills/{skill}/references/guide.md": "Reference: cite exact locators.\n",
        }
    )
    folder.mkdir(parents=True, exist_ok=True)
    components = []
    for rel, content in files.items():
        data = content.encode("utf-8") if isinstance(content, str) else content
        path = folder.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if rel in skip_components:
            continue
        license_expression = licenses.get(rel) if licenses is not None else "MIT"
        components.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "license_expression": license_expression,
                "origin_url": "https://example.invalid/synthetic",
                "origin_revision": "0" * 40,
                "modified": False,
            }
        )
    sidecar = {
        "schema_version": 1,
        "name": name,
        "version": version,
        "description": "Synthetic package for tests.",
        "skills": sorted({"/".join(p.split("/")[:2]) for p in files if p.startswith("skills/")}),
        "license_expression": package_license,
        "components": components,
        "capabilities": capabilities if capabilities is not None else ["project_read"],
        "recipes": recipes if recipes is not None else [RECIPE],
        **(extra_sidecar or {}),
    }
    (folder / "prometheos-package.json").write_text(json.dumps(sidecar, indent=2), "utf-8")
    return folder


def zip_members(
    archive: Path, members: list[tuple[str, bytes]], symlink: str | None = None
) -> Path:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "../../outside")
    return archive


def zip_folder(folder: Path, archive: Path) -> Path:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder).as_posix())
    return archive


def import_package(svc: Any, source: Path) -> dict[str, Any]:
    plan = svc.skill_import_plan(source)
    return svc.skill_import(source, plan["plan_sha256"])


def install(svc: Any, ref: str, host: str, target: Path, project_id: str | None = None,
            resolve: str | None = None) -> dict[str, Any]:  # fmt: skip
    plan = svc.skill_install(ref, host, target, None, resolve, authority=OWNER_ACT)["plan"]
    return svc.skill_install(
        ref, host, target, plan["plan_sha256"], resolve, project_id, authority=OWNER_ACT
    )
