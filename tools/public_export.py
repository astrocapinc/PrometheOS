"""Build the public export: a fresh one-commit repository from an allow-list (decision 0072).

    uv run python tools/public_export.py --out <new folder outside the repository>
        [--author "Name <email>"] [--names-from dogfood] [--denylist FILE] [--allow-dirty]
        [--unchecked]

It copies the tracked files under the allow-list below from this checkout (the working tree
must be clean unless ``--allow-dirty``), writes ``PUBLIC-EXPORT.json``, runs
``tools/privacy_scan.py`` over the result, and only when the scan is clean makes a new Git
repository there with one commit on ``main``. It never adds a remote and never pushes. It
refuses to run without the owner's denylist and the research names to harvest, unless
``--unchecked`` says so explicitly.

The commit's author and committer default to a placeholder identity, so the export carries
none of this machine's Git identity. The owner sets their own before publishing
(``git commit --amend --reset-author``, adding ``-s`` for a DCO sign-off of their own).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import privacy_scan  # noqa: E402

PLACEHOLDER_AUTHOR = "PrometheOS maintainers <maintainers@prometheos.invalid>"
MARKER = "PUBLIC-EXPORT.json"
# Tracked paths that enter the public repository: a file, or a folder ending in "/".
INCLUDE = (
    ".github/", ".gitattributes", ".gitignore", ".python-version",
    "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "README.md", "CONTRIBUTING.md",
    "SECURITY.md", "CODE_OF_CONDUCT.md", "AGENTS.md", "START-HERE.md",
    "pyproject.toml", "uv.lock", "build-constraints.txt",
    "checkpoints.json", "requirements.json", "design-inputs.json", "package-manifest.json",
    "acceptance/", "contracts/", "docs/", "fixtures/", "schemas/", "spec/", "src/",
    "templates/", "tests/", "tools/", "hub/", "desktop/app/",
    # The rights register: the notices and the register check read it.
    "development/dependencies.json",
)  # fmt: skip
# Inside the allow-list, still left out: screenshots of a developer's screen (the hub UI/UX
# pass supplies public ones), and the CP19.1 spike.
EXCLUDE = ("hub/screenshots/",)


def git(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def selected(paths: list[str]) -> list[str]:
    def wanted(rel: str) -> bool:
        if any(rel.startswith(e) for e in EXCLUDE):
            return False
        return any(rel == i or (i.endswith("/") and rel.startswith(i)) for i in INCLUDE)

    return sorted(p for p in paths if wanted(p))


def tree_digest(root: Path, rels: list[str]) -> str:
    lines = [f"{rel}\t{hashlib.sha256((root / rel).read_bytes()).hexdigest()}" for rel in rels]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def split_identity(identity: str) -> tuple[str, str]:
    name, _, rest = identity.partition("<")
    if not rest.endswith(">") or not name.strip():
        raise SystemExit(f"--author must look like 'Name <email>': {identity!r}")
    return name.strip(), rest[:-1].strip()


def owner_checks(
    denylists: list[Path], names_from: list[Path], unchecked: bool
) -> tuple[set[str], set[str]]:
    """The owner's denylist terms and harvested research names. Both are required (review F3):
    without them the scan sees only generic patterns, so the export refuses unless the caller
    says ``--unchecked`` explicitly."""
    paths = denylists or (
        [privacy_scan.DEFAULT_DENYLIST] if privacy_scan.DEFAULT_DENYLIST.is_file() else []
    )
    terms = privacy_scan.load_denylist(paths)
    names: set[str] = set()
    for folder in names_from:
        if not folder.is_dir():
            raise SystemExit(f"--names-from {folder} is not a folder")
        names |= privacy_scan.harvest_names(folder)
    if not unchecked:
        if not terms:
            raise SystemExit(
                f"no owner denylist ({privacy_scan.DEFAULT_DENYLIST} or --denylist); "
                "pass --unchecked to export with the generic rules only"
            )
        if not names:
            raise SystemExit(
                "no research names (--names-from <folder of real research>); "
                "pass --unchecked to export without them"
            )
    return terms, names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--author", default=PLACEHOLDER_AUTHOR)
    parser.add_argument("--names-from", type=Path, action="append", default=[])
    parser.add_argument("--denylist", type=Path, action="append", default=[])
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--unchecked",
        action="store_true",
        help="export without the owner's denylist or research names (generic rules only)",
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.is_relative_to(ROOT):
        raise SystemExit("the export folder must be outside the repository")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    if not args.allow_dirty and git("status", "--porcelain").strip():
        raise SystemExit("the working tree has uncommitted changes (or pass --allow-dirty)")
    terms, names = owner_checks(args.denylist, args.names_from, args.unchecked)
    rels = selected([p for p in git("ls-files", "-z").split("\0") if p])
    rels = [r for r in rels if (ROOT / r).is_file()]
    out.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    marker = {
        "kind": "prometheos-public-export",
        "source_commit": git("rev-parse", "HEAD").strip(),
        "source_dirty": bool(git("status", "--porcelain").strip()),
        "files": len(rels),
        "tree_sha256": tree_digest(out, rels),
        "note": "A fresh export of the private repository by tools/public_export.py; the "
        "private repository keeps the history, the receipts and the evidence (development/).",
    }
    (out / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    findings = privacy_scan.scan(out, terms, names)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.rule}: {f.detail}")
    print(
        f"privacy scan: {len(findings)} finding(s); {len(terms)} denylist term(s), "
        f"{len(names)} research name(s), {len(rels) + 1} files"
    )
    if findings:
        print(f"FAIL: not committed; inspect {out}", file=sys.stderr)
        return 1
    name, email = split_identity(args.author)
    # In UTC: the machine's offset would hint at where the owner lives (review F5).
    when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    git("init", "-q", "-b", "main", cwd=out)
    git("add", "-A", cwd=out)
    git(
        "-c", f"user.name={name}", "-c", f"user.email={email}",
        "commit", "-q", "-m", "PrometheOS public source, version 0.1.0.dev0", cwd=out, env=env,
    )  # fmt: skip
    commit = git("rev-parse", "HEAD", cwd=out).strip()
    print(f"PASS: {out} is a new repository with one commit {commit} ({len(rels) + 1} files)")
    print("Nothing was pushed. Review it, set your identity, then add a remote yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
