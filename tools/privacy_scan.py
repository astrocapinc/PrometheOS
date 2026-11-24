"""Privacy and secret scan for a public tree (decision 0072).

    uv run python tools/privacy_scan.py <folder> [--denylist FILE] [--names-from DIR] [--json]

Scans every file under the folder (``.git`` excepted) and exits 1 on any finding:

- ``home-path``: a user's home folder (``C:\\Users\\<name>``, ``/c/Users/<name>``,
  ``/home/<name>``, ``/Users/<name>``) unless the name is a generic example (``someone``,
  ``researcher``, ``you`` ...);
- ``email``: an e-mail address outside the reserved example domains, except in licence and
  notice files, which quote third-party authors by design;
- ``secret``: private keys and the token formats of GitHub, Anthropic, OpenAI, AWS, Slack and
  Google, and JSON Web Tokens;
- ``denylist``: each line of the denylist files, matched case-insensitively. The default is
  ``local/privacy-denylist.txt`` (git-ignored), where the owner keeps their own name, e-mail,
  institution and folder names, so that the public scanner never contains them;
- ``research-name``: the distinctive file and folder names under ``--names-from`` (for example
  the git-ignored ``dogfood/`` folder of real research), so no real project name leaks.

A line that contains ``privacy-scan: allow`` is skipped; use it only for synthetic test values.
Findings print masked (the first two characters, then ``***``), never the full value.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DENYLIST = ROOT / "local" / "privacy-denylist.txt"
ALLOW_MARK = "privacy-scan: allow"
MAX_BYTES = 64 * 1024 * 1024  # the desktop core's largest binary is about 10 MB

GENERIC_USERS = {
    "someone", "researcher", "you", "your-name", "yourname", "user", "username", "<user>",
    "<you>", "<name>", "x", "example", "alice", "bob", "public", "default", "me",
    "test", "tester", "runner", "runneradmin", "name", "dev", "student", "foo", "jdoe",
    "%username%", "$env", "...", "*", "pc",
    # Synthetic people of the redaction tests, and Emscripten's default home in pdf.js.
    "u", "r", "john", "jos", "web_user",
}  # fmt: skip
HOME = re.compile(
    r"(?i)(?:\b[a-z]:[\\/]{1,2}|/[a-z]/|(?<![\w.:/])/)(?:users|home)[\\/]{1,2}([^\\/\s\"'<>`|:;,)\]}]+)"
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
EXAMPLE_DOMAINS = re.compile(
    r"(?i)(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(example|test|invalid|localhost)|"
    r"users\.noreply\.github\.com)$"
)
SECRETS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "JSON Web Token": re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"),
}  # fmt: skip
NOTICE_FILE = re.compile(r"(?i)(licen[cs]e|notice|copying|authors)")
# Third-party text where an address is not ours to judge: lockfiles quote packages' own
# messages, and minified bundles hold strings that only look like addresses.
EMAIL_EXEMPT = re.compile(r"(^|/)(package-lock\.json|uv\.lock)$|(^|/)prometheos/hub/static/assets/")
# Synthetic values that tests need on purpose: (path, rule, a substring of the line). The
# strings are split so that this file does not match its own rule.
ALLOWED = (
    ("tests/test_cp17_review_r2.py", "secret", r"-----BEGIN RSA PRIVATE " r"KEY-----\nMIIEow"),
    ("tests/test_cp17_review_r2.py", "secret", r"-----BEGIN OPENSSH PRIVATE " r"KEY-----\nb3Blbn"),
)
# Harvested names under these folders are tool-generated, not research (Simulink's caches and
# project metadata, agent settings).
GENERATED_PARTS = {"slprj", "resources", "__pycache__", ".claude", ".git", ".vscode"}
# A harvested name is distinctive when it has a separator, a digit or an inner capital and is
# long enough: "pump_test_rig", "ValveMap_Series" or "rIG4", not "envelope".
DISTINCTIVE = re.compile(r"^(?=.{4,})(?=.*(?:[_\- ]|\d|[a-z][A-Z])).*$")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str


def mask(value: str) -> str:
    return value[:2] + "***" if len(value) > 2 else "***"


def harvest_names(folder: Path) -> set[str]:
    """Distinctive stems (and their full names) of the files and folders under ``folder``."""
    names = set()
    for path in folder.rglob("*"):
        if GENERATED_PARTS & set(path.relative_to(folder).parts[:-1]) or path.name[:1] in "._":
            continue
        if DISTINCTIVE.match(path.stem):
            names |= {path.stem.lower(), path.name.lower()}
    return names


def allowed(rel: str, rule: str, line: str) -> bool:
    # Paths are repository-relative; a scan of a subfolder sees their tails.
    return any(p.endswith(rel) and rule == r and s in line for p, r, s in ALLOWED)


def load_denylist(paths: list[Path]) -> set[str]:
    terms = set()
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            term = raw.strip()
            if term and not term.startswith("#"):
                terms.add(term.lower())
    return terms


def scan_text(rel: str, text: str, terms: set[str], names: set[str]) -> list[Finding]:
    found: list[Finding] = []
    notice = bool(NOTICE_FILE.search(Path(rel).name))
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        for match in HOME.finditer(line):
            user = match.group(1).strip(".").lower()
            if user and user not in GENERIC_USERS and not user.startswith(("<", "{", "$", "%")):
                found.append(Finding(rel, number, "home-path", mask(match.group(1))))
        if not notice and not EMAIL_EXEMPT.search(rel):
            for match in EMAIL.finditer(line):
                local = match.group(0).split("@", 1)[0]
                # git@host is an SSH address, and user:secret@host a URL's credentials (the
                # secret rules, not this one, judge those).
                in_url = match.start() > 0 and line[match.start() - 1] in ":/"
                if local != "git" and not in_url and not EXAMPLE_DOMAINS.search(match.group(1)):
                    found.append(Finding(rel, number, "email", mask(match.group(0))))
        for label, pattern in SECRETS.items():
            if pattern.search(line) and not allowed(rel, "secret", line):
                found.append(Finding(rel, number, "secret", label))
        lower = line.lower()
        for term in terms:
            if term in lower:
                found.append(Finding(rel, number, "denylist", mask(term)))
        # A short name counts only as a word of its own, not inside a longer identifier.
        hits = [
            name
            for name in names
            if name in lower
            and (len(name) >= 6 or re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lower))
        ]
        # One finding per name, not one more for each shorter name inside it (a stem and its
        # file name).
        for name in hits:
            if not any(name != other and name in other for other in hits):
                found.append(Finding(rel, number, "research-name", mask(name)))
    return found


def scan(
    folder: Path,
    terms: set[str],
    names: set[str],
    rules: frozenset[str] | None = None,
    only: Callable[[str], bool] | None = None,
) -> list[Finding]:
    """Scan every file under ``folder``. ``rules`` keeps only those rules' findings (a release
    build applies the owner's denylist to third-party files, whose maintainers' addresses and
    build paths are theirs to publish); ``only`` limits the scan to the paths it accepts."""
    findings = [
        f
        for f in _scan(folder, terms, names, only)
        if rules is None or f.rule in rules or f.rule == "too-large-to-scan"
    ]
    return findings


def _scan(
    folder: Path, terms: set[str], names: set[str], only: Callable[[str], bool] | None
) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if ".git" in path.relative_to(folder).parts or not path.is_file():
            continue
        if only is not None and not only(rel):
            continue
        # The path itself can leak a name, binary or not.
        findings += [Finding(rel, 0, f.rule, f.detail) for f in scan_text(rel, rel, terms, names)]
        if path.stat().st_size > MAX_BYTES:
            findings.append(Finding(rel, 0, "too-large-to-scan", f"{path.stat().st_size} bytes"))
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            # Binary: its single-byte and its UTF-16 strings (Windows keeps paths in UTF-16).
            text = data.decode("latin-1") + "\n" + data.decode("utf-16-le", errors="ignore")
        else:
            text = data.decode("utf-8", errors="replace")
        findings += scan_text(rel, text, terms, names)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--denylist", type=Path, action="append", default=[])
    parser.add_argument("--names-from", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    denylists = args.denylist or ([DEFAULT_DENYLIST] if DEFAULT_DENYLIST.is_file() else [])
    terms = load_denylist(denylists)
    names: set[str] = set()
    for folder in args.names_from:
        names |= harvest_names(folder)
    findings = scan(args.folder.resolve(), terms, names)
    summary = {
        "folder": args.folder.name,
        "denylist_terms": len(terms),
        "research_names": len(names),
        "findings": [asdict(f) for f in findings],
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: {f.rule}: {f.detail}")
        verdict = "FAIL" if findings else "PASS"
        print(
            f"{verdict}: {len(findings)} finding(s); {len(terms)} denylist term(s), "
            f"{len(names)} research name(s) checked"
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
