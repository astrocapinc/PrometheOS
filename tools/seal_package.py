"""Refresh a specification seal after an explicitly reviewed design amendment.

This is an integrity inventory, not a signature or implementation acceptance.
Never refresh it just to hide unexpected changes. Mutable development evidence is
excluded so implementation can proceed without changing the design fingerprint.

The sealed inventory is the existing manifest's file list; a new design file enters
only when named with --add. The version is read from checkpoints.json. Each sealed
version is archived under development/spec-history/<version>/ so receipts bound to it
stay verifiable (decision 0008). Before editing sealed files for a new version, run
with --archive-current (it checks the files still match the seal); sealing a new
version refuses while the previous one is not archived byte for byte. ``--drop`` leaves a
sealed file out of the new seal (spec 1.5.2 drops the root README, decision 0072).
"""
import argparse
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
ARCHIVED = ("checkpoints.json", "schemas/receipt.schema.json", "schemas/impact.schema.json")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--add", action="append", default=[], help="repository-relative design file to seal")
parser.add_argument("--drop", action="append", default=[], help="sealed file to leave out of the new seal")
parser.add_argument("--archive-current", action="store_true", help="archive the sealed version, then stop")
args = parser.parse_args()


def archive_dir(version: str) -> Path:
    return root / "development" / "spec-history" / version


def write_archive(version: str, files: dict[str, bytes]) -> None:
    target_dir = archive_dir(version)
    for rel, data in files.items():
        target = target_dir / rel
        if target.exists() and target.read_bytes() != data:
            raise SystemExit(f"spec {version} is already archived with different {rel}; bump spec_version")
    for rel, data in files.items():
        (target_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (target_dir / rel).write_bytes(data)


manifest_bytes = (root / "package-manifest.json").read_bytes()
previous = json.loads(manifest_bytes.decode("utf-8"))
if args.archive_current:
    for item in previous["files"]:
        if hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise SystemExit(f"{item['path']} no longer matches the seal; archive before editing")
    write_archive(previous["spec_version"], {"package-manifest.json": manifest_bytes, **{rel: (root / rel).read_bytes() for rel in ARCHIVED}})
    print(f"Archived sealed spec {previous['spec_version']}.")
    raise SystemExit(0)

paths = [item["path"] for item in previous["files"] if item["path"] not in set(args.drop)]
for extra in args.add:
    rel = Path(extra).as_posix()
    if rel not in paths:
        paths.append(rel)
files = []
for rel in paths:
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise SystemExit(f"cannot seal {rel}: not a file inside the package")
    files.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
spec_version = json.loads((root / "checkpoints.json").read_text(encoding="utf-8"))["spec_version"]
if spec_version != previous["spec_version"]:
    kept = archive_dir(previous["spec_version"]) / "package-manifest.json"
    if not kept.is_file() or kept.read_bytes() != manifest_bytes:
        raise SystemExit(
            f"spec {previous['spec_version']} is not archived; its files have changed since sealing, "
            "so it can no longer be archived faithfully (run --archive-current before editing)"
        )
manifest_text = json.dumps({"schema_version": 1, "spec_version": spec_version, "description": "Design artifact integrity only; not product acceptance.", "files": files}, indent=2) + "\n"
write_archive(spec_version, {"package-manifest.json": manifest_text.encode("utf-8"), **{rel: (root / rel).read_bytes() for rel in ARCHIVED}})
(root / "package-manifest.json").write_bytes(manifest_text.encode("utf-8"))
print(f"Sealed {len(files)} specification files at spec {spec_version} (archived in development/spec-history/{spec_version}). Run verify_package.py next.")
