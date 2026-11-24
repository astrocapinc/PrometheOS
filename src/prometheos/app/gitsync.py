"""Overleaf Git connector (A01, CP9.4; decision 0012 items 14-20 and its integration review
sections).

PrometheOS drives the system ``git`` with explicit argument lists, a timeout and a clean
environment. Every invocation overrides the configuration that can run a program (hooks,
``core.fsmonitor``, ``core.sshCommand``, pager, ``core.askPass``, external diff,
attribute-selected diff/merge drivers and clean/smudge filters, credential helpers other
than Git Credential Manager), and a repository whose own (local or worktree)
configuration names any such program is refused before anything runs. Overleaf
authentication is Git Credential Manager's business: PrometheOS never asks for, reads,
logs, stores or puts in a URL the Overleaf token, and refuses credential-bearing remote
URLs.

Every fetch and push names the clone's one origin URL explicitly, never the remote name.
Configuration from any scope that could send them elsewhere (URL rewrites, push URLs, a
second origin URL, a remote named like a URL, transport programs) is refused, and missing
objects are never fetched lazily from another remote.

Pull is fetch, preview, then integrate only by fast-forward or a clean merge; anything
else stops with both sides intact. Push sends exactly the previewed commits (no tags),
only with the owner's exact-plan capability, and never forces. Comments, tracked changes
and Overleaf history labels are not synchronised.
"""

from __future__ import annotations

import fnmatch
import functools
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prometheos.app import sources, workspace
from prometheos.app.reads import Caller
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.ids import new_request_id
from prometheos.domain.models import PrometheosError
from prometheos.domain.text import invisible
from prometheos.sources.paths import normalize_relative, resolve_in_root
from prometheos.store import capabilities, db
from prometheos.store.project import ProjectStore, Txn

LOCAL_TIMEOUT = 30.0
REMOTE_TIMEOUT = 180.0
REMOTE = "origin"
# Every fetch names the one origin URL with this refspec, never the remote name, so none of
# the remote's own settings (another URL, a fetch refspec, an upload program, a proxy)
# applies (integration review INT2-F2).
TRACKING = f"+refs/heads/*:refs/remotes/{REMOTE}/*"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_LISTED = 200
PUSH_OPERATION = "overleaf_push"
LIMITATIONS = [
    "Overleaf comments, tracked changes and history labels are not synchronised.",
    "Pull integrates only a fast-forward or a clean merge; anything else stops untouched.",
    "Push sends exactly the previewed commits with the owner's approval and never forces.",
    "Fetch and push use only the clone's one origin URL; a second URL, URL rewrites or a "
    "partial clone are refused.",
    "Authentication is Git Credential Manager's; PrometheOS never handles the Overleaf token.",
]
# Settings are matched on the parsed name (_parts), never by a pattern over the whole name:
# the section and the key are compared case-insensitively, as git does, against these
# (section, key) pairs, where the key may be a glob ("*" for every key). The subsection
# plays no part, so an empty one is matched like any other (pre-check round 2 INT2G-F1:
# ``url\..+\.`` missed a rewrite with an empty base).
#
# Settings that can run a program or pull in other configuration. Refused in the clone's
# own configuration: the local and worktree scopes, and any scope git reports other than
# the owner's system and global files and PrometheOS's own command line (integration
# review INT2-F4).
RISKY_LOCAL: frozenset[tuple[str, str]] = frozenset(
    {
        ("include", "*"),
        ("includeif", "*"),
        ("credential", "*"),
        *(
            ("core", key)
            for key in (
                "hookspath",
                "fsmonitor",
                "sshcommand",
                "pager",
                "askpass",
                "editor",
                "gitproxy",
                "alternaterefscommand",
                "attributesfile",
                "worktree",
                "externaldiff",
            )
        ),
        ("diff", "external"),
        ("diff", "command"),
        ("diff", "textconv"),
        ("merge", "driver"),
        ("mergetool", "*"),
        ("difftool", "*"),
        ("filter", "*"),
        ("pager", "*"),
        ("gpg", "*"),
        ("commit", "gpgsign"),
        ("tag", "gpgsign"),
        ("push", "gpgsign"),
        ("sequence", "editor"),
        ("ssh", "variant"),
        ("gc", "recentobjectshook"),
        ("http", "*sslkey*"),
        ("http", "*sslcert*"),
        ("http", "*cookiefile*"),
        ("http", "*proxy*"),
        # TLS trust and name resolution: together (another address for the Overleaf host
        # with verification off or a planted CA) they would hand a push and the token to
        # another server. The owner's own global settings (a corporate CA) stay allowed.
        ("http", "sslverify"),
        ("http", "sslcainfo"),
        ("http", "sslcapath"),
        ("http", "schannelusesslcainfo"),
        ("http", "curloptresolve"),
    }
)
OWNER_SCOPES = ("system", "global")
# Refused from every scope, the owner's own files included: each could send a fetch or an
# approved push somewhere other than the one origin URL, run a transport program, or make
# git fetch objects from another place (integration reviews CP9 F1, INT2-F1, INT2-F4;
# pre-check round 2). Every key of the url section is refused (insteadOf and pushInsteadOf
# are its only keys). Remotes named like a URL or path are refused as well (_url_like).
REDIRECTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("url", "*"),
        ("remote", "pushurl"),
        ("remote", "uploadpack"),
        ("remote", "receivepack"),
        ("remote", "vcs"),
        ("remote", "promisor"),
        ("extensions", "partialclone"),
        ("promisor", "acceptfromserver"),
    }
)
MASKED = "<url-or-path>"
REFUSED = (
    "PrometheOS refuses this clone's Git configuration: the settings listed can run a "
    "program, or send a fetch or push somewhere other than the one origin URL. Find them "
    "with git config --show-origin --show-scope --list, which also names the file that "
    "holds each, and remove each with git config --<scope> --unset-all <name>, or, for a "
    "value from an included file, git config --file <that file> --unset-all <name>; for "
    "several origin URLs, remove remote.origin.url wherever it is listed, then set the one "
    "URL with git remote set-url origin <url>. Then retry"
)
# Every call passes --attr-source (git 2.41); the clean-merge preview needs merge-tree to
# accept it (fixed in 2.43); the status view relies on GIT_NO_LAZY_FETCH (2.45). Older or
# unrecognised versions are refused (pre-check round 2 INT2G-F5). Tested with 2.53.
GIT_FLOOR = (2, 45, 0)


def _matched(rules: Iterable[tuple[str, str]], section: str, key: str) -> bool:
    return any(section == s and fnmatch.fnmatchcase(key, k) for s, k in rules)


def _parts(name: str) -> tuple[str, str | None, str]:
    """Split a configuration name as git does: the section ends at the first dot, the key
    starts after the last one, and the subsection (case kept) lies between. The
    subsection is None when the name has none and "" when it is empty (``url..insteadof``)."""
    section, _, rest = name.partition(".")
    subsection, dot, key = rest.rpartition(".")
    return section.lower(), (subsection if dot else None), key.lower()


def _url_like(name: str) -> bool:
    """A remote name that git could match against a URL or path given on the command line:
    git looks such an argument up as a remote name first, so ``[remote "<URL>"] url = X``
    redirects a fetch or push named by that URL (integration review INT2-F1). Every URL or
    path PrometheOS accepts contains ':' or a backslash or starts with '/', and
    ``git remote add`` can create none of these names."""
    return ":" in name or "\\" in name or name.startswith("/")


def _shown(scope: str, name: str, bare_local: bool = False) -> str:
    """A refused setting as it is reported: the name only (never the value). A subsection
    that could hold a URL, and so a token, or an invisible character is masked, and an
    empty one is shown as ``""``."""
    section, subsection, key = _parts(name)
    if subsection is None:
        text = f"{section}.{key}"
    elif subsection == "":
        text = f'{section}."".{key}'
    elif any(c in "@:/\\" or invisible(c) for c in subsection):
        text = f"{section}.{MASKED}.{key}"
    else:
        text = f"{section}.{subsection}.{key}"
    return text if bare_local and scope == "local" else f"{scope}:{text}"


def _invalid(message: str, **details: Any) -> PrometheosError:
    return PrometheosError("validation_error", message, details)


# ----- remote URLs -----------------------------------------------------------------------------


def check_remote_url(url: str) -> str:
    """Return the URL if it is an https URL without credentials, or a repository on a local
    drive (an existing absolute path, or a file URL without a host); refuse anything else
    without echoing it (it may hold a token)."""
    value = url.strip()
    refused = PrometheosError(
        "policy_denied",
        "remote URL refused: use https://git.overleaf.com/<project> without a user name, "
        "password or token (the token belongs in Git Credential Manager), or a repository "
        "on a local drive (network shares are refused)",
        {},
    )
    # No space and no invisible character anywhere (pre-check round 2 INT2G-F2): the
    # destination is shown to the owner at approval and must read as what it is.
    if not value or len(value) > 2048 or any(c == " " or invisible(c) for c in value):
        raise refused
    if "::" in value or value.lower().startswith(("ext:", "fd:")):
        raise refused
    # UNC paths (\\server\share, //server/share) and device paths (\\?\..., \\.\...) name
    # another computer or bypass path rules; checking or using one reaches that computer,
    # where Windows may sign in on its own. Refused before any filesystem or git access
    # (pre-check round 3 R2-L2; spec/08).
    if re.match(r"^[\\/]{2}", value):
        raise refused
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        # URLs are ASCII, hosts included (an internationalised host in its punycode form),
        # so no letter can stand in for a look-alike. Local paths may hold other letters.
        if not value.isascii():
            raise refused
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() not in ("https", "file"):
            raise refused
        if "@" in parts.netloc or parts.username or parts.password:
            raise refused
        # A file URL with any host (localhost included), or whose path is a network path,
        # names another computer as a UNC path does (pre-check round 3 R2-L2).
        if parts.scheme.lower() == "file" and (
            parts.netloc or re.match(r"^/?[\\/]{2}", urllib.parse.unquote(parts.path))
        ):
            raise refused
        if parts.query or parts.fragment:
            raise refused
        if parts.scheme.lower() == "https" and not parts.hostname:
            raise refused
        return value
    if re.match(r"^[^/\\]+@[^/\\]+:", value):  # scp-like ssh syntax
        raise refused
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise refused
    return value


def redact(text: str) -> str:
    """Remove any user-info from URLs in git output (defence in depth)."""
    return re.sub(r"(https?://)[^/\s@]+@", r"\1", text)


def _decoded(data: bytes, exact: bool) -> str:
    """UTF-8 text; with universal newlines unless ``exact`` (see Git.run)."""
    text = data.decode("utf-8", "replace")
    return text if exact else text.replace("\r\n", "\n").replace("\r", "\n")


LOG = logging.getLogger(__name__)
# A git command at least this slow is logged, by its subcommand, duration and exit code only
# (never its arguments: URLs, paths, messages). On a loaded host a stalled git child is otherwise
# invisible in the hub's log (decision 0028, review cp10-browser R4-F2).
SLOW_GIT = 2.0


def _verb(args: Iterable[str]) -> str:
    """git's subcommand in ``args``, skipping leading ``-c name=value`` overrides."""
    rest = list(args)
    while rest[:1] == ["-c"]:
        rest = rest[2:]
    return rest[0] if rest else "?"


def _timed(
    argv: list[str], verb: str, timeout: float, **options: Any
) -> subprocess.CompletedProcess[bytes]:
    """``subprocess.run`` of one git command; a slow or timed-out one is logged by its verb."""
    started = time.monotonic()
    try:
        done: subprocess.CompletedProcess[bytes] = subprocess.run(argv, timeout=timeout, **options)
    except subprocess.TimeoutExpired:
        LOG.warning("git %s did not finish within %g s", verb, timeout)
        raise
    elapsed = time.monotonic() - started
    if elapsed >= SLOW_GIT:
        LOG.warning("git %s took %.1f s (exit %s)", verb, elapsed, done.returncode)
    return done


@functools.lru_cache(maxsize=8)
def _git_version(executable: str) -> tuple[int, int, int] | None:
    """The version of the git at this path, or None when it cannot be determined."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = _timed(
            [executable, "--version"],
            "--version",
            LOCAL_TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            creationflags=creation,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.match(
        r"git version (\d+)\.(\d+)(?:\.(\d+))?", done.stdout.decode("utf-8", "replace")
    )
    if done.returncode != 0 or found is None:
        return None
    return int(found[1]), int(found[2]), int(found[3] or 0)


# ----- runner ----------------------------------------------------------------------------------


class Git:
    """One repository, driven with safe argv, overrides and a clean environment."""

    def __init__(self, directory: Path, home: Path) -> None:
        self.directory = directory
        self.home = home
        executable = shutil.which("git")
        if executable is None:
            raise PrometheosError("unsupported", "git is not installed or not on PATH", {})
        version = _git_version(executable)
        if version is None or version < GIT_FLOOR:
            floor = ".".join(map(str, GIT_FLOOR))
            found = ".".join(map(str, version)) if version else "of an unrecognised version"
            raise PrometheosError(
                "unsupported",
                f"PrometheOS needs git {floor} or newer; this git is {found}",
                {},
            )
        self.executable = executable
        self.neutral = home / "tools" / "git-neutral"
        (self.neutral / "hooks").mkdir(parents=True, exist_ok=True)
        (self.neutral / "template").mkdir(parents=True, exist_ok=True)
        attributes = self.neutral / "attributes"
        if not attributes.exists():
            attributes.write_text("", encoding="utf-8")

    def environment(self) -> dict[str, str]:
        keep = (
            "SystemRoot",
            "SYSTEMROOT",
            "windir",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "ProgramData",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "CommonProgramFiles",
            "USERNAME",
            "USERDOMAIN",
            "COMPUTERNAME",
            "TEMP",
            "TMP",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        )
        env = {k: os.environ[k] for k in keep if os.environ.get(k)}
        git_dir = str(Path(self.executable).parent)
        system = os.environ.get("SystemRoot", r"C:\Windows")
        env["PATH"] = os.pathsep.join(
            [git_dir, str(Path(system) / "System32")] if sys.platform == "win32" else [git_dir]
        )
        if sys.platform != "win32":
            env["PATH"] += os.pathsep + "/usr/bin:/bin"
        env.setdefault("HOME", env.get("USERPROFILE", str(Path.home())))
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        # Never fetch a missing object lazily from a promisor remote: the guard refuses
        # promisor settings, but a status view still runs local commands (git 2.45+).
        env["GIT_NO_LAZY_FETCH"] = "1"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        return env

    def _pairs(self) -> list[tuple[str, str]]:
        hooks = str(self.neutral / "hooks")
        return [
            ("core.hooksPath", hooks),
            ("core.fsmonitor", "false"),
            ("core.sshCommand", ""),
            ("core.pager", "cat"),
            ("core.askPass", ""),
            ("core.editor", ""),
            ("core.attributesFile", str(self.neutral / "attributes")),
            ("core.alternateRefsCommand", ""),
            ("core.quotePath", "false"),
            ("diff.external", ""),
            ("credential.helper", ""),
            ("credential.helper", "manager"),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            ("protocol.file.allow", "always"),
            ("commit.gpgSign", "false"),
            ("tag.gpgSign", "false"),
            ("push.gpgSign", "false"),
            ("submodule.recurse", "false"),
            ("fetch.recurseSubmodules", "false"),
            # No bundles from a configured URI before a fetch: a fetch reads nothing but
            # the one origin URL.
            ("fetch.bundleURI", ""),
            ("init.templateDir", str(self.neutral / "template")),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("color.ui", "false"),
            ("advice.detachedHead", "false"),
        ]

    def overrides(self) -> list[str]:
        argv: list[str] = []
        for key, value in self._pairs():
            argv += ["-c", f"{key}={value}"]
        return argv

    def override_keys(self) -> frozenset[str]:
        return frozenset(key.lower() for key, _ in self._pairs())

    def run(
        self,
        *args: str,
        timeout: float = LOCAL_TIMEOUT,
        check: bool = True,
        cwd: Path | None = None,
        exact: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run git. Its output is read as UTF-8 text with universal newlines, or, with
        ``exact``, character for character: git accepts a carriage return inside a
        configuration name, and a newline translation would cut the name there."""
        argv = [
            self.executable,
            "--no-pager",
            f"--attr-source={EMPTY_TREE}",
            *self.overrides(),
            *args,
        ]
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            raw = _timed(
                argv,
                _verb(args),
                timeout,
                cwd=cwd or self.directory,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                creationflags=creation,
            )
        except subprocess.TimeoutExpired as exc:
            raise PrometheosError(
                "io_error", f"git {args[0]} did not finish within {timeout:g} s", {}
            ) from exc
        done = subprocess.CompletedProcess(
            argv, raw.returncode, _decoded(raw.stdout, exact), _decoded(raw.stderr, exact)
        )
        if check and done.returncode != 0:
            raise PrometheosError(
                "io_error",
                f"git {args[0]} failed",
                {"exit": done.returncode, "stderr": redact(done.stderr.strip())[-2000:]},
            )
        return done

    def config(self) -> list[tuple[str, str, str | None]]:
        """Every configuration entry git sees here, as (scope, name, value): system,
        global, local, worktree or command, with included files under the scope that
        includes them. Fails closed: if git cannot list them (git older than 2.26 has no
        --show-scope; a file does not parse), nothing runs (integration review INT2-I1)."""
        done = self.run("config", "--show-scope", "--list", "-z", check=False, exact=True)
        tokens = done.stdout.split("\0")
        if done.returncode != 0 or tokens[-1] != "" or len(tokens) % 2 != 1:
            raise PrometheosError(
                "policy_denied",
                "PrometheOS could not read this clone's Git configuration, so it runs nothing "
                "there (git 2.26 or newer is needed, and every configuration file must parse)",
                {"exit": done.returncode},
            )
        entries: list[tuple[str, str, str | None]] = []
        for index in range(0, len(tokens) - 1, 2):
            name, newline, value = tokens[index + 1].partition("\n")
            entries.append((tokens[index], name, value if newline else None))
        return entries

    def redirects(self, entries: list[tuple[str, str, str | None]] | None = None) -> list[str]:
        """Settings from *any* scope that could send a fetch or an approved push somewhere
        other than the one origin URL, or run a transport program: URL rewrites, push URLs,
        upload/receive programs, remote helpers, promisor remotes, and remotes named like a
        URL or path (integration reviews CP9 F1, INT2-F1, INT2-F4). The command scope may
        hold only PrometheOS's own overrides; anything else there came through the
        environment and is refused (integration review INT2-I1)."""
        own = self.override_keys()
        found = set()
        for scope, name, _ in self.config() if entries is None else entries:
            section, subsection, key = _parts(name)
            if scope == "command":
                if name.lower() not in own:
                    found.add(_shown(scope, name))
            elif _matched(REDIRECTS, section, key) or (
                section == "remote" and subsection is not None and _url_like(subsection)
            ):
                found.add(_shown(scope, name))
        return sorted(found)

    def local_config_risks(
        self, entries: list[tuple[str, str, str | None]] | None = None
    ) -> list[str]:
        """Settings that can run a program or pull in other configuration, from the clone's
        own configuration (every scope but the owner's system and global files and
        PrometheOS's command line). Local settings are listed by name, others with their
        scope. ``info/attributes`` is read where git reads it, through the repository's
        common directory (pre-check round 2 INT2G-I1)."""
        risky = set()
        for scope, name, _ in self.config() if entries is None else entries:
            section, _subsection, key = _parts(name)
            if scope not in (*OWNER_SCOPES, "command") and _matched(RISKY_LOCAL, section, key):
                risky.add(_shown(scope, name, bare_local=True))
        found = sorted(risky)
        located = self.run("rev-parse", "--git-path", "info/attributes").stdout.strip()
        attributes = Path(located) if os.path.isabs(located) else self.directory / located
        if attributes.is_file() and re.search(
            r"\b(filter|diff|merge)=", attributes.read_text("utf-8", "replace")
        ):
            found.append("info/attributes (filter/diff/merge drivers)")
        return found

    def origin_urls(
        self, entries: list[tuple[str, str, str | None]] | None = None
    ) -> list[tuple[str, str]]:
        """Every value of remote.origin.url, from every scope, with its scope. The values
        are never shown (one may hold a token); exactly one is accepted (INT2-F2)."""
        return [
            (scope, value or "")
            for scope, name, value in (self.config() if entries is None else entries)
            if scope != "command" and _parts(name) == ("remote", REMOTE, "url")
        ]


# ----- repository of a paper -------------------------------------------------------------------


def _repository(conn: Any, project_id: str, caller: Caller, paper_id: str, home: Path) -> Any:
    ws = workspace.resolve(conn, project_id, caller, paper_id)
    directory = ws.directory
    dot_git = directory / ".git"
    if not dot_git.is_dir():
        raise PrometheosError(
            "validation_error",
            "the workspace folder is not a Git clone (link it with `paper overleaf link`)",
            {},
        )
    git = Git(directory, home)
    top = git.run("rev-parse", "--show-toplevel").stdout.strip()
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(directory)):
        raise PrometheosError(
            "validation_error", "the workspace folder must be the top of its Git clone", {}
        )
    return ws, git, workspace._bindings(conn, ws.alias)


def _problems(git: Git) -> tuple[list[str], list[str]]:
    """The refused settings (names only) and every origin URL (never shown), from one
    reading of the configuration (pre-check round 2 INT2G-I1). Several origin URLs are
    reported with the scopes that hold them (INT2G-F4)."""
    entries = git.config()
    risks = sorted({*git.local_config_risks(entries), *git.redirects(entries)})
    origins = git.origin_urls(entries)
    if len(origins) > 1:
        scopes = ", ".join(sorted({scope for scope, _ in origins}))
        risks.append(f"remote.{REMOTE}.url ({len(origins)} values in {scopes}; keep exactly one)")
    return risks, [url for _, url in origins]


def _guard(git: Git) -> str:
    """Refuse a clone whose configuration could run a program or send a fetch or push
    elsewhere, and return its one origin URL, validated credential-free. That URL is the
    destination bound into every plan digest (review cp9-a round 3 O1), and every fetch and
    push names it explicitly, never the remote name (integration reviews CP9 F1, INT2-F1,
    INT2-F2), so the owner approves exactly where the commits go."""
    risks, urls = _problems(git)
    if risks:
        raise PrometheosError("policy_denied", REFUSED, {"settings": risks[:50]})
    if not urls:
        raise PrometheosError("validation_error", "the clone has no 'origin' remote", {})
    return check_remote_url(urls[0]).rstrip("/")


def _accepted(urls: list[str]) -> str | None:
    """The destination a status view shows: the single origin URL when it is acceptable."""
    if len(urls) != 1:
        return None
    try:
        return check_remote_url(urls[0]).rstrip("/")
    except PrometheosError:
        return None


def _fetch(git: Git, url: str) -> None:
    """Fetch from the one validated origin URL, named explicitly with PrometheOS's own
    refspec into refs/remotes/origin/*: no remote setting (another URL, a fetch refspec,
    an upload program, a proxy) applies (integration review INT2-F2)."""
    git.run(
        "fetch",
        "--no-recurse-submodules",
        "--no-tags",
        "--end-of-options",
        url,
        TRACKING,
        timeout=REMOTE_TIMEOUT,
    )


def _branch(git: Git) -> str:
    branch = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    if not branch:
        raise PrometheosError(
            "validation_error", "the clone is not on a branch (detached HEAD)", {}
        )
    return branch


def _oid(git: Git, ref: str) -> str | None:
    done = git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    value = done.stdout.strip()
    return value or None


def _commits(git: Git, spec: str) -> list[dict[str, Any]]:
    done = git.run(
        "log", f"--max-count={MAX_LISTED}", "--format=%H%x1f%an%x1f%aI%x1f%s", spec, "--"
    )
    out = []
    for line in done.stdout.splitlines():
        oid, author, date, subject = [*line.split("\x1f"), "", "", "", ""][:4]
        out.append({"oid": oid, "author": author, "date": date, "subject": subject[:300]})
    return out


def _changed_files(git: Git, a: str, b: str) -> list[dict[str, str]]:
    done = git.run("diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", a, b, "--")
    items = done.stdout.split("\x00")
    out: list[dict[str, str]] = []
    index = 0
    while index < len(items) - 1 and len(out) < MAX_LISTED:
        status = items[index]
        if status.startswith(("R", "C")):
            out.append({"status": status[0], "path": items[index + 2], "from": items[index + 1]})
            index += 3
        else:
            out.append({"status": status[:1], "path": items[index + 1]})
            index += 2
    return out


def _local_changes(git: Git, limit: int | None = MAX_LISTED) -> list[str]:
    """Paths with local changes (``git status --porcelain=v1 -z``). A rename or copy entry
    is followed by its original path, which is listed too."""
    done = git.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    items = done.stdout.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        paths.append(item[3:])
        if item[0] in "RC" or item[1] in "RC":
            if index < len(items) and items[index]:
                paths.append(items[index])
            index += 1
    return paths if limit is None else paths[:limit]


OUTSIDE_SCOPE_NOTICE = "changes outside your scope exist: owner review required"
SCOPED_FIELDS = (
    "local_changes",
    "incoming_files",
    "both_changed",
    "conflicts",
    "files",
    "uncommitted_not_pushed",
)


def _registered(ws: Any, registered: dict[str, dict[str, Any]], path: str) -> dict[str, Any] | None:
    return registered.get(ws.root_relative(path).casefold()) if path else None


def _name_of(entry: Any) -> list[str]:
    if isinstance(entry, dict):
        return [str(entry.get("path", "")), str(entry.get("from", ""))]
    return [str(entry)]


def _scoped(
    view: dict[str, Any], caller: Caller, ws: Any, registered: dict[str, dict[str, Any]]
) -> None:
    """Review cp9-a round 2 R2-3. Agents never see names of files registered above their
    scope: such entries are removed and one count-free notice says owner review is needed.
    The owner's view names every restricted or private file explicitly."""
    removed = False
    flagged: dict[str, str] = {}
    for key in SCOPED_FIELDS:
        entries = view.get(key)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            sources_of = [_registered(ws, registered, name) for name in _name_of(entry)]
            above = [
                src
                for src in sources_of
                if src is not None and not caller.may_see(src["sensitivity"])
            ]
            if above:
                removed = True
                continue
            for name, src in zip(_name_of(entry), sources_of, strict=True):
                if src is not None and src["sensitivity"] != "normal":
                    flagged[name] = src["sensitivity"]
            kept.append(entry)
        view[key] = kept
    if caller.scope == "owner":
        view["restricted_files"] = [
            {"path": name, "sensitivity": level} for name, level in sorted(flagged.items())
        ]
    else:
        view["scope_notice"] = OUTSIDE_SCOPE_NOTICE if removed else None


def status(store: ProjectStore, home: Path, caller: Caller, paper_id: str) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        ws, git, registered = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    risks, urls = _problems(git)
    url = _accepted(urls)
    branch = _branch(git)
    head = _oid(git, "HEAD")
    tracking = _oid(git, f"refs/remotes/{REMOTE}/{branch}")
    ahead = behind = None
    if head and tracking:
        counts = git.run("rev-list", "--left-right", "--count", f"HEAD...{tracking}").stdout.split()
        ahead, behind = int(counts[0]), int(counts[1])
    view = {
        "workspace": ws.describe(),
        "remote": {"name": REMOTE, "url": url, "url_accepted": url is not None},
        "branch": branch,
        "head": head,
        "remote_head_as_of_last_fetch": tracking,
        "ahead": ahead,
        "behind": behind,
        "local_changes": _local_changes(git),
        "blocked_settings": risks,
        "limitations": LIMITATIONS,
    }
    _scoped(view, caller, ws, registered)
    return view


# ----- link (clone and bind) -------------------------------------------------------------------


def _writable(function: Any, path: str, _: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _preflight(target: Path, url: str, home: Path) -> None:
    """Before anything is fetched, check the configuration the clone will see: the owner's
    system and global files with their includes, evaluated for this very folder, and the
    origin URL the clone will record. The clone is itself a fetch, so a rewrite, a second
    origin URL or an upload program from another scope must stop it first (integration
    review INT2-F2). The probe repository is removed again; the clone starts empty."""
    existed = target.exists()
    try:
        Git(target.parent, home).run("init", "-q", str(target))
        probe = Git(target, home)
        probe.run("config", "--local", f"remote.{REMOTE}.url", url)
        _guard(probe)
    finally:
        if (target / ".git").exists():
            shutil.rmtree(target / ".git", onexc=_writable)
        if not existed and target.is_dir() and not any(target.iterdir()):
            target.rmdir()


def link(
    store: ProjectStore,
    home: Path,
    caller: Caller,
    paper_id: str,
    url: str,
    root_alias: str,
    folder: str,
    main: str,
    actor: str,
    max_rank: int,
) -> dict[str, Any]:
    checked = check_remote_url(url)
    rel = normalize_relative(folder)
    conn = store.connect(readonly=True)
    try:
        workspace.require_paper(conn, caller, paper_id)
        settings, _ = sources._settings(conn, store.project_id)
    finally:
        conn.close()
    root = sources.root_path(settings, root_alias)
    resolved = resolve_in_root(root, rel)
    target = resolved.path
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise _invalid("the clone folder must not exist or must be empty", folder=rel)
    if len(Path(rel).parts) > 1:
        resolve_in_root(root, str(Path(rel).parent.as_posix()))
        target.parent.mkdir(parents=True, exist_ok=True)
    _preflight(target, checked, home)
    git = Git(target.parent, home)
    git.run(
        "clone",
        "--no-recurse-submodules",
        "--origin",
        REMOTE,
        "--",
        checked,
        str(target),
        timeout=REMOTE_TIMEOUT,
        cwd=target.parent,
    )
    clone = Git(target, home)
    _guard(clone)
    main_rel = normalize_relative(main)
    if not (target / main_rel).is_file():
        raise _invalid(
            "the clone has no such main file; bind the workspace with `paper workspace bind`",
            main=main_rel,
        )
    bound = workspace.bind(
        store, caller, paper_id, root_alias, f"{resolved.relative}/{main_rel}", actor, max_rank
    )
    return {
        "cloned": resolved.relative,
        "bind": bound,
        "status": status(store, home, caller, paper_id),
    }


# ----- pull ------------------------------------------------------------------------------------


def _pull_plan(git: Git, branch: str) -> dict[str, Any]:
    head = _oid(git, "HEAD")
    incoming = _oid(git, f"refs/remotes/{REMOTE}/{branch}")
    plan: dict[str, Any] = {
        "branch": branch,
        "head": head,
        "incoming_head": incoming,
        "incoming_commits": [],
        "incoming_files": [],
        "local_commits": [],
        "local_changes": _local_changes(git),
        "both_changed": [],
        "conflicts": [],
    }
    if incoming is None or head is None:
        plan["kind"] = "no_remote_branch"
        plan["explanation"] = f"the remote has no branch {branch} (fetch found nothing to pull)"
        return plan
    if (
        head == incoming
        or git.run("merge-base", "--is-ancestor", incoming, head, check=False).returncode == 0
    ):
        plan["kind"] = "up_to_date"
        plan["explanation"] = "nothing new on Overleaf"
        return plan
    base = git.run("merge-base", head, incoming).stdout.strip()
    plan["base"] = base
    plan["incoming_commits"] = _commits(git, f"{head}..{incoming}")
    plan["incoming_files"] = _changed_files(git, base, incoming)
    incoming_paths = {f["path"] for f in plan["incoming_files"]} | {
        f["from"] for f in plan["incoming_files"] if "from" in f
    }
    overlap = sorted(p for p in plan["local_changes"] if p in incoming_paths)
    if base == head:
        kind = "fast_forward"
    else:
        plan["local_commits"] = _commits(git, f"{incoming}..{head}")
        local_paths = {f["path"] for f in _changed_files(git, base, head)}
        plan["both_changed"] = sorted(local_paths & incoming_paths)
        merged = git.run(
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            head,
            incoming,
            check=False,
        )
        if merged.returncode == 0:
            kind = "merge"
        elif merged.returncode == 1:
            kind = "diverged_conflict"
            plan["conflicts"] = [p for p in merged.stdout.splitlines()[1:] if p][:MAX_LISTED]
        else:
            raise PrometheosError("io_error", "git merge-tree failed", {})
    if overlap and kind in ("fast_forward", "merge"):
        kind = "blocked_local_changes"
        plan["conflicts"] = overlap
    plan["kind"] = kind
    plan["explanation"] = {
        "fast_forward": "Overleaf has new commits and you have none: a fast-forward",
        "merge": "both sides have commits that merge without conflicts",
        "diverged_conflict": "both sides changed the same lines; nothing was changed here. "
        "Resolve on one side (e.g. in Overleaf, or commit and merge manually), then pull again",
        "blocked_local_changes": "unsaved-to-Git local edits touch files that changed on "
        "Overleaf; commit or set them aside first. Nothing was changed",
    }[kind]
    return plan


BINDING_KEYS = (
    "remote_url",
    "kind",
    "branch",
    "head",
    "incoming_head",
    "base",
    "local_head",
    "remote_head",
    "force",
)


def _digest(plan: dict[str, Any]) -> str:
    """Bind what the action does, not how it is shown: the kind, branch and exact commit
    OIDs (which determine every file) plus the pushed commit list. File lists are views
    that differ by caller scope (review cp9-a round 2 R2-3), so an agent-prepared and an
    owner-reviewed plan share one digest without hashing restricted names."""
    bound = {k: plan.get(k) for k in BINDING_KEYS}
    bound["commits"] = [c["oid"] for c in plan.get("commits", [])]
    bound["incoming_commits"] = [c["oid"] for c in plan.get("incoming_commits", [])]
    bound["local_commits"] = [c["oid"] for c in plan.get("local_commits", [])]
    return canonical_sha256(bound)


def pull_preview(
    store: ProjectStore, home: Path, caller: Caller, paper_id: str, fetch: bool = True
) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        ws, git, registered = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    url = _guard(git)
    branch = _branch(git)
    if fetch:
        _fetch(git, url)
    plan = _pull_plan(git, branch)
    plan["remote_url"] = url
    _scoped(plan, caller, ws, registered)
    return {
        "workspace": ws.describe(),
        "plan": plan,
        "digest": _digest(plan),
        "applicable": plan["kind"] in ("fast_forward", "merge"),
        "limitations": LIMITATIONS,
    }


def _identity(git: Git) -> list[str]:
    name = git.run("config", "user.name", check=False).stdout.strip()
    email = git.run("config", "user.email", check=False).stdout.strip()
    extra: list[str] = []
    if not name:
        extra += ["-c", "user.name=PrometheOS"]
    if not email:
        extra += ["-c", "user.email=prometheos@localhost"]
    return extra


def pull_apply(
    store: ProjectStore,
    home: Path,
    caller: Caller,
    paper_id: str,
    digest: str,
    actor: str,
) -> dict[str, Any]:
    preview = pull_preview(store, home, caller, paper_id, fetch=False)
    if preview["digest"] != digest:
        raise PrometheosError(
            "revision_conflict",
            "the repository changed since the preview; preview the pull again",
            {"preview": preview},
        )
    plan = preview["plan"]
    if not preview["applicable"]:
        raise PrometheosError(
            "revision_conflict", plan["explanation"], {"kind": plan["kind"], "plan": plan}
        )
    conn = store.connect(readonly=True)
    try:
        _, git, _registered_sources = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    if plan["kind"] == "fast_forward":
        git.run("merge", "--ff-only", "--no-edit", plan["incoming_head"])
    else:
        done = git.run(
            *_identity(git),
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            "Merge Overleaf changes (PrometheOS pull)",
            plan["incoming_head"],
            check=False,
        )
        if done.returncode != 0:
            git.run("merge", "--abort", check=False)
            raise PrometheosError(
                "revision_conflict",
                "the merge did not complete; it was aborted and both sides are intact",
                {"stderr": redact(done.stderr.strip())[-2000:]},
            )
    snapshot = workspace.snapshot(store, caller, paper_id, actor)
    return {
        "applied": plan["kind"],
        "head": _oid(git, "HEAD"),
        "snapshot": {"recorded": snapshot["recorded"], "versions": snapshot["versions"]},
        "limitations": LIMITATIONS,
    }


# ----- commit and push -------------------------------------------------------------------------


def commit(
    store: ProjectStore, home: Path, caller: Caller, paper_id: str, message: str
) -> dict[str, Any]:
    text = " ".join(message.split())
    if not text or len(text) > 500:
        raise _invalid("a commit message of 1 to 500 characters is required")
    conn = store.connect(readonly=True)
    try:
        ws, git, registered = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    _guard(git)
    changes = _local_changes(git, limit=None)
    if not changes:
        raise _invalid("there are no local changes to commit")
    for name in changes:
        source = _registered(ws, registered, name)
        if source is not None and not caller.may_see(source["sensitivity"]):
            raise PrometheosError(
                "policy_denied",
                "owner scope is required: the change set includes files registered above "
                "agent scope",
                {},
            )
    git.run("add", "--all", "--", ".")
    git.run(*_identity(git), "commit", "--no-verify", "--no-edit", "-m", text)
    return {"head": _oid(git, "HEAD"), "message": text}


def _push_plan(git: Git, branch: str) -> dict[str, Any]:
    head = _oid(git, "HEAD")
    remote = _oid(git, f"refs/remotes/{REMOTE}/{branch}")
    plan: dict[str, Any] = {
        "branch": branch,
        "local_head": head,
        "remote_head": remote,
        "commits": [],
        "files": [],
        "uncommitted_not_pushed": _local_changes(git),
        "force": False,
    }
    if head is None:
        plan["kind"] = "nothing_to_push"
    elif (
        remote is not None
        and git.run("merge-base", "--is-ancestor", remote, head, check=False).returncode != 0
    ):
        plan["kind"] = "blocked_behind"
    elif remote == head:
        plan["kind"] = "nothing_to_push"
    else:
        plan["kind"] = "fast_forward_push"
        spec = f"{remote}..{head}" if remote else head
        plan["commits"] = _commits(git, spec)
        plan["files"] = _changed_files(git, remote or EMPTY_TREE, head)
    plan["explanation"] = {
        "nothing_to_push": "Overleaf already has every local commit",
        "blocked_behind": "Overleaf has commits you do not have; pull first. Nothing is forced",
        "fast_forward_push": "these commits would be added to Overleaf (fast-forward, no force)",
    }[plan["kind"]]
    return plan


def push_preview(store: ProjectStore, home: Path, caller: Caller, paper_id: str) -> dict[str, Any]:
    conn = store.connect(readonly=True)
    try:
        ws, git, registered = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    url = _guard(git)
    branch = _branch(git)
    _fetch(git, url)
    plan = _push_plan(git, branch)
    plan["remote_url"] = url
    _scoped(plan, caller, ws, registered)
    return {
        "workspace": ws.describe(),
        "plan": plan,
        "digest": _digest(plan),
        "applicable": plan["kind"] == "fast_forward_push",
        "capability_operation": PUSH_OPERATION,
        "limitations": LIMITATIONS,
    }


def push_apply(
    store: ProjectStore,
    home: Path,
    caller: Caller,
    paper_id: str,
    digest: str,
    secret: capabilities.Secret,
    actor: str,
) -> dict[str, Any]:
    """Push exactly the previewed plan. The owner's capability (operation overleaf_push,
    bound to the plan digest) is consumed in a commit that also audits the push."""
    preview = push_preview(store, home, caller, paper_id)
    if preview["digest"] != digest:
        raise PrometheosError(
            "revision_conflict",
            "the push plan changed (new local commits or new commits on Overleaf); review the "
            "new plan",
            {"preview": preview},
        )
    plan = preview["plan"]
    if not preview["applicable"]:
        raise PrometheosError("revision_conflict", plan["explanation"], {"kind": plan["kind"]})

    def handler(txn: Txn) -> None:
        capabilities.consume(
            txn.conn,
            secret=secret,
            project_id=store.project_id,
            operation=PUSH_OPERATION,
            digest=digest,
        )
        txn.audit("overleaf_push_authorized", paper_id, None)
        txn.result(paper_id, None)

    authorised = store.commit(
        request_id=new_request_id(),
        request_sha256=canonical_sha256({"op": "overleaf_push", "digest": digest}),
        expected_seq=None,
        actor=actor,
        handler=handler,
        max_rank=caller.rank,
    )
    conn = store.connect(readonly=True)
    try:
        _, git, _registered_sources = _repository(conn, store.project_id, caller, paper_id, home)
    finally:
        conn.close()
    # Checked again right before the push: a configuration edit made while the preview
    # fetched (a remote named by this URL, a rewrite) must not steer the approved push.
    if _guard(git) != plan["remote_url"]:
        raise PrometheosError(
            "revision_conflict",
            "the destination changed since the preview; nothing was pushed",
            {"authorised": authorised},
        )
    done = git.run(
        "push",
        "--porcelain",
        "--no-recurse-submodules",
        # Exactly the previewed commits: no tags, even with push.followTags configured.
        "--no-follow-tags",
        "--end-of-options",
        # The explicit URL bound in the approved plan, never the remote name: a name would
        # follow remote.<name>.pushurl (integration review CP9 F1). A remote *named* by this
        # URL would capture it, and the guard just above refuses one (INT2-F1).
        plan["remote_url"],
        f"{plan['local_head']}:refs/heads/{plan['branch']}",
        timeout=REMOTE_TIMEOUT,
        check=False,
    )
    if done.returncode != 0:
        raise PrometheosError(
            "revision_conflict",
            "Overleaf refused the push (nothing was forced); fetch, pull and review again",
            {"stderr": redact(done.stderr.strip())[-2000:], "authorised": authorised},
        )
    try:
        _fetch(git, plan["remote_url"])
    except PrometheosError as exc:
        # The push may well have happened; without the check it cannot be confirmed, so
        # this is the same unknown outcome as a failed check (pre-check round 3 R2-I2).
        raise PrometheosError(
            "io_error",
            "git reported the push as done, but checking the approved destination afterwards "
            "failed, so the outcome is unknown; nothing is reported as pushed. Check the "
            "destination before pushing again",
            {
                "outcome": "unknown",
                "authorised": authorised,
                "stderr": exc.details.get("stderr", exc.message),
            },
        ) from exc
    # The approved destination must now hold the pushed commit (an ancestor test: a
    # collaborator may push right after). Otherwise the commits may have gone elsewhere,
    # and the outcome is reported as unknown, never as done (pre-check round 2 INT2G-F1).
    remote_head = _oid(git, f"refs/remotes/{REMOTE}/{plan['branch']}")
    if remote_head is None or (
        remote_head != plan["local_head"]
        and git.run(
            "merge-base", "--is-ancestor", plan["local_head"], remote_head, check=False
        ).returncode
        != 0
    ):
        raise PrometheosError(
            "io_error",
            "git reported the push as done, but the approved destination does not hold the "
            "pushed commit, so the commits may have gone elsewhere; nothing is reported as "
            "pushed. Check this clone's Git configuration (git config --show-scope --list) "
            "before pushing again",
            {"outcome": "unknown", "authorised": authorised},
        )
    return {
        "pushed": plan["commits"],
        "remote_head": remote_head,
        "authorised": authorised,
        "limitations": LIMITATIONS,
    }


def grant_push(store: ProjectStore, digest: str, label: str) -> capabilities.Grant:
    """Owner channel only (interactive CLI confirmation or the paired hub session)."""
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            return capabilities.issue(
                conn,
                project_id=store.project_id,
                operation=PUSH_OPERATION,
                digest=digest,
                ttl_seconds=120,
                max_uses=1,
                label=label,
            )
    finally:
        conn.close()
