"""The durable pending-deletion list of a project store (decision 0031).

A file that must go but cannot be removed right now (on Windows a file another handle holds
open cannot be removed) is listed here, and every later sweep tries again. The list is
``cache/stale-caches.txt``, one entry per line, in exactly one of three forms (anything else is
not an entry):

* a cache file: its bare name in ``cache/`` (letters, digits, ``.``, ``_`` and ``-``);
* a retained media file: ``media/<xx>/<sha256>`` (lowercase hex; the content-addressed key);
* a file left in a finished recorder session: ``staging/rec-<uuid>/<name>``.

It is the ONE such list: purge, the transcription caches, the recorder and the run service all
use it. It is swept when a project is opened, at the start of every recovery pass, at the start
and the end of every transcription run, when a ``transcribe.materialized`` block ends, and by
every purge.

The list is untrusted input (a crash can leave it zero-filled, and it is a plain file anyone can
edit): it is read as bytes, at most :data:`MAX_LIST_BYTES` of it; every line that is not an
entry is dropped (and counted in the log, never named); the next locked change rewrites it from
its valid entries. Nothing here raises.

Writers never lose one another's entries: every change is a read-merge-write under a short
cross-process lock (``<store>/pending-deletion.lock``, released by the operating system when its
holder dies), written durably (flushed and synced) and published by an atomic rename. That lock
is only held for the change itself: no database access and no other lock is taken while holding
it. A temporary file a writer that died left beside the list (``<store>/.pending-*.tmp``) is
removed by the next locked change.

A sweep is opportunistic: it never waits for another operation. If the list lock, the project's
admission gate or the maintenance lock is busy, it leaves what needs them for a later sweep.

A media file is content-addressed, so the same recording imported again after a purge is the
same file: a listed media file that a ``media`` row refers to again is live data and is never
removed (its entry is dropped instead). Media entries are only swept while holding the project's
maintenance lock, which every media import holds from staging its file until its reference
commits: no import can start using a file between the check and the removal. A listed recorder
file is only removed when its session is finished (imported or discarded) or gone.

Derived audio in ``cache/`` names the recording it came from: a shared entry carries its full
digest (``audio-<sha256>.bin``, ``pcm-<sha256>.raw``) and a private or unfinished file the first
:data:`PREFIX` hex digits (:func:`private_name`). A sweep that has the store also removes derived
files of a recording the store no longer retains, however they came to be there
(:func:`collect_unretained`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from prometheos.domain.models import PrometheosError
from prometheos.store.lock import _try_lock, _unlock, maintenance_lock

if TYPE_CHECKING:
    from prometheos.store.project import ProjectStore

LOG = logging.getLogger(__name__)
LIST_NAME = "stale-caches.txt"
LOCK_NAME = "pending-deletion.lock"
TEMP_GLOB = ".pending-*.tmp"
MAX_LIST_BYTES = 1024 * 1024  # never read (or write) more of the list than this
MAX_ENTRIES = 5000
PREFIX = 16  # hex digits of the recording's digest in a private file's name (see PRIVATE)
ENTRY = re.compile(r"^(?:audio|pcm)-([0-9a-f]{64})\.(?:bin|raw)$")
PRIVATE = re.compile(r"^\.(?:audio|pcm)-([0-9a-f]{16})-[0-9a-f]{32}\.tmp$")
CACHE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
MEDIA_ENTRY = re.compile(r"media/([0-9a-f]{2})/([0-9a-f]{64})")
SESSION = r"rec-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
STAGING_ENTRY = re.compile(rf"staging/({SESSION})/([A-Za-z0-9._-]{{1,128}})")
FINISHED_SESSION_STATES = ("imported", "discarded")


def private_name(kind: str, sha: str) -> str:
    """The name of a private (unpublished or unfinished) ``kind`` file ("audio" or "pcm")
    derived from the recording with digest ``sha``.

    It says which recording it came from, so :func:`collect_unretained` can remove it once that
    recording is purged even if its writer died; and it is shorter than the shared entries'
    names, so it never pushes a cache path over the Windows path limit that the entries fit."""
    return f".{kind}-{sha[:PREFIX]}-{uuid.uuid4().hex}.tmp"


def media_entry(key: str) -> str:
    return f"media/{key}"


def _list_path(store_dir: Path) -> Path:
    return store_dir / "cache" / LIST_NAME


def _resolve(store_dir: Path, entry: str) -> Path | None:
    """The file an entry names, or ``None`` when ``entry`` is not one (see the module text)."""
    if CACHE_NAME.fullmatch(entry):
        if entry.strip(".") == "" or entry == LIST_NAME:
            return None
        return store_dir / "cache" / entry
    media = MEDIA_ENTRY.fullmatch(entry)
    if media is not None and media.group(2).startswith(media.group(1)):
        return store_dir / "media" / media.group(1) / media.group(2)
    staging = STAGING_ENTRY.fullmatch(entry)
    if staging is not None and staging.group(2).strip(".") != "":
        return store_dir / "staging" / staging.group(1) / staging.group(2)
    return None


def _kind(entry: str) -> str:
    return entry.partition("/")[0] if "/" in entry else "cache"


def _load(store_dir: Path) -> tuple[list[str], int]:
    """The list's valid entries (in order, without repeats) and how many lines were not
    entries. Reads at most :data:`MAX_LIST_BYTES` (a longer list counts its cut-off line as
    dropped). Never raises: an unreadable list reads as empty."""
    try:
        with open(_list_path(store_dir), "rb") as handle:
            data = handle.read(MAX_LIST_BYTES + 1)
    except OSError:
        return [], 0
    dropped = 0
    if len(data) > MAX_LIST_BYTES:
        data = data[: data.rfind(b"\n", 0, MAX_LIST_BYTES) + 1]
        dropped += 1
    entries: dict[str, None] = {}
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            dropped += 1
            continue
        if _resolve(Path("."), text) is None:
            dropped += 1
            continue
        entries[text] = None
    return list(entries), dropped


def _read(store_dir: Path) -> list[str]:
    return _load(store_dir)[0]


def _capped(entries: list[str]) -> list[str]:
    """At most :data:`MAX_ENTRIES`, media and recorder entries first (nothing else would find
    those files again; cache files have the backstop)."""
    if len(entries) <= MAX_ENTRIES:
        return entries
    ordered = sorted(entries, key=lambda e: _kind(e) == "cache")
    LOG.warning("%d pending deletions over the bound were dropped", len(entries) - MAX_ENTRIES)
    return ordered[:MAX_ENTRIES]


def _durable(path: Path, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write(store_dir: Path, entries: list[str]) -> None:
    """Replace the list (callers hold the list lock). May raise ``OSError``."""
    path = _list_path(store_dir)
    entries = _capped(entries)
    if not entries:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # open elsewhere just now: leave it empty instead
            _durable(path, b"")
        return
    data = "".join(f"{entry}\n" for entry in entries).encode("ascii")
    tmp = store_dir / f".pending-{uuid.uuid4().hex}.tmp"
    try:
        _durable(tmp, data)
        os.replace(tmp, path)
        return
    except OSError:  # the list is open elsewhere: rewrite it in place (the lock keeps writers out)
        pass
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)
    _durable(path, data)


def _drop_dead_temps(store_dir: Path) -> None:
    """Remove what a writer that died between writing and renaming left (callers hold the list
    lock, so no live writer has a temporary file in progress)."""
    with contextlib.suppress(OSError):
        for leftover in store_dir.glob(TEMP_GLOB):
            with contextlib.suppress(OSError):
                leftover.unlink(missing_ok=True)


@contextlib.contextmanager
def _locked(store_dir: Path, timeout: float) -> Iterator[bool]:
    """Hold the list lock for a short change; ``False`` when it could not be had in time
    (``timeout`` 0: one attempt)."""
    try:
        fd = os.open(store_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield False
        return
    try:
        if os.fstat(fd).st_size == 0:
            # Give the lock byte a body. Another opener of the new file may have written and
            # locked it first: on Windows the write then fails (a locked range), and it is not
            # needed (int6b; a lost write here used to drop add()'s entries).
            with contextlib.suppress(OSError):
                os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.01)
        try:
            yield True
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def add(store_dir: Path, entries: Iterable[str]) -> None:
    """List files that could not be removed now, merged with what is listed already. A writer
    waits (briefly) for the list lock: an entry must not be lost. Never raises."""
    try:
        wanted = [e for e in dict.fromkeys(entries) if _resolve(store_dir, e) is not None]
        if not wanted:
            return
        (store_dir / "cache").mkdir(exist_ok=True)
        with _locked(store_dir, timeout=5.0) as locked:
            if locked:
                _drop_dead_temps(store_dir)
                _write(store_dir, list(dict.fromkeys([*_read(store_dir), *wanted])))
                return
        # The lock was not had in five seconds (its holder is suspended inside a change): an
        # append is never lost to another append, only to a rewrite happening at this moment.
        with open(_list_path(store_dir), "ab") as handle:
            handle.write("".join(f"{entry}\n" for entry in wanted).encode("ascii"))
    except Exception as exc:  # never fatal: a purge that has committed, or a run's end, calls this
        LOG.warning("could not record files as pending deletion (%s)", type(exc).__name__)


def _unlink(path: Path) -> bool | None:
    """``True`` removed (or absent), ``False`` held (retry later), ``None`` names nothing that
    can be removed (drop the entry)."""
    try:
        if path.is_dir():
            return None
        path.unlink(missing_ok=True)
        return True
    except PermissionError:  # Windows: in use (sharing violation) or access denied
        return False
    except (OSError, ValueError):
        return None


def remove_or_list(store_dir: Path, entries: Iterable[str]) -> list[str]:
    """Remove each named cache or recorder file now; the ones that cannot be removed now are
    listed for the sweep and returned. An absent file is done. Media entries are never removed
    here (their references must be checked first): list them with :func:`add`. Never
    raises."""
    failed: list[str] = []
    for entry in dict.fromkeys(entries):
        path = _resolve(store_dir, entry)
        if path is None or _kind(entry) == "media":
            continue
        if _unlink(path) is False:
            failed.append(entry)
    if failed:
        add(store_dir, failed)
    return failed


def _classify(names: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Derived names: {name: full digest} for shared entries, {name: digest prefix} for
    private files."""
    entries: dict[str, str] = {}
    privates: dict[str, str] = {}
    for name in names:
        if match := ENTRY.match(name):
            entries[name] = match.group(1)
        elif match := PRIVATE.match(name):
            privates[name] = match.group(1)
    return entries, privates


def clear_cache(
    store_dir: Path, store: ProjectStore | None = None, purged: Iterable[str] = ()
) -> list[str]:
    """What a purge removes from ``cache/``: every file except the derived files of recordings
    the store still retains (a running transcription's unfinished or unpublished output, a
    paused run's decode: their owners or the backstop remove them in time). Files that cannot
    be removed now are listed, and returned.

    Whether a recording is retained is read from the store after the purge's commit; when it
    cannot be read, the derived files of the ``purged`` digests are removed and other derived
    files are kept (the backstop decides later). Never raises."""
    try:
        with os.scandir(store_dir / "cache") as listing:
            names = [entry.name for entry in listing if entry.is_file()]
    except OSError:
        return []
    names = [name for name in names if name != LIST_NAME]
    entries, privates = _classify(names)
    found = None
    if store is not None and (entries or privates):
        found = _retained(store, set(entries.values()), set(privates.values()), wait=True)
    if found is None:
        gone = set(purged)
        gone_prefixes = {sha[:PREFIX] for sha in gone}
        keep = {n for n, sha in entries.items() if sha not in gone}
        keep |= {n for n, prefix in privates.items() if prefix not in gone_prefixes}
    else:
        kept, kept_prefixes = found
        keep = {n for n, sha in entries.items() if sha in kept}
        keep |= {n for n, prefix in privates.items() if prefix in kept_prefixes}
    failed: list[str] = []
    unnamed = 0
    for name in names:
        if name in keep:
            continue
        if _unlink(store_dir / "cache" / name) is False:
            if _resolve(store_dir, name) is None:
                unnamed += 1
            else:
                failed.append(name)
    if unnamed:
        LOG.warning("%d cache files in use could not be listed for deletion", unnamed)
    add(store_dir, failed)
    return failed


def remove_tree(store_dir: Path, directory: Path) -> int:
    """Remove a recorder session folder (``staging/rec-<uuid>``) and list the files that could
    not be removed now (another handle holds them) for the sweeps; returns how many were
    listed. Never raises."""
    with contextlib.suppress(OSError):
        shutil.rmtree(directory, ignore_errors=True)
    return list_leftovers(store_dir, directory)


def list_leftovers(store_dir: Path, directory: Path) -> int:
    """List the files still in a recorder session folder that was just removed (they were held
    by another handle) for the sweeps; returns how many were listed. Never raises."""
    try:
        if not directory.is_dir():
            return 0
        leftovers = [
            f"staging/{directory.name}/{path.name}"
            for path in directory.iterdir()
            if path.is_file()
        ]
    except OSError:
        return 0
    wanted = [entry for entry in leftovers if _resolve(store_dir, entry) is not None]
    add(store_dir, wanted)
    return len(wanted)


def count(store_dir: Path) -> int:
    return len(_read(store_dir))


def sweep(store_dir: Path, store: ProjectStore | None = None) -> int:
    """Remove what the list names and nothing holds any more; return how many are left.

    Cache files are disposable and always swept. Media and recorder files are only swept when
    ``store`` is given (whether a ``media`` row refers to a file again, or whether a session is
    finished, needs the database); media files also need the maintenance lock (see the module
    text). With ``store``, derived files in ``cache/`` of recordings the store no longer retains
    are collected too (:func:`collect_unretained`). Opportunistic: nothing here waits for another
    operation. Never raises."""
    try:
        _sweep_list(store_dir, store)
        if store is not None:
            collect_unretained(store_dir, store)
    except Exception:  # never fatal: every project open runs this
        LOG.warning("the pending-deletion sweep stopped early")
    return count(store_dir)


@contextlib.contextmanager
def _media_guard(store_dir: Path) -> Iterator[bool]:
    """The maintenance lock, if it can be had at once. Refused (another maintenance operation or
    a media import holds it, or this thread has a database connection open) means: not now."""
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(maintenance_lock(store_dir, timeout=0))
            ok = True
        except PrometheosError:
            ok = False
        yield ok


@contextlib.contextmanager
def _no_guard() -> Iterator[bool]:
    yield False


def _facts(
    store: ProjectStore, keys: list[str], sessions: list[str]
) -> tuple[set[str], set[str]] | None:
    """Which media keys a ``media`` row refers to, and which recorder sessions are not finished
    (open sessions: their files are live); ``None`` if that cannot be read at once."""
    try:
        conn = store.connect(readonly=True, wait=False)
    except Exception:  # busy, unreadable, not openable: not now
        return None
    try:
        referenced: set[str] = set()
        if keys:
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT storage_key FROM media WHERE storage_key IN ({marks})",  # noqa: S608
                keys,
            ).fetchall()
            referenced = {str(row[0]) for row in rows}
        unfinished: set[str] = set()
        if sessions:
            marks = ",".join("?" for _ in sessions)
            rows = conn.execute(
                f"SELECT id FROM recording_sessions WHERE id IN ({marks}) "  # noqa: S608
                "AND state NOT IN (?, ?)",
                [*sessions, *FINISHED_SESSION_STATES],
            ).fetchall()
            unfinished = {str(row[0]) for row in rows}
        return referenced, unfinished
    except sqlite3.Error:  # unreadable now: leave the files alone
        return None
    finally:
        conn.close()


def _sweep_list(store_dir: Path, store: ProjectStore | None) -> None:
    listed, dropped = _load(store_dir)
    dead = any(store_dir.glob(TEMP_GLOB))
    if not listed and not dropped and not dead:
        if _list_path(store_dir).exists():  # an emptied list that could not be removed
            with _locked(store_dir, timeout=0) as locked:
                if locked and not _read(store_dir):
                    _write(store_dir, [])
        return
    keys = [entry.removeprefix("media/") for entry in listed if _kind(entry) == "media"]
    sessions = sorted(
        {entry.split("/")[1].removeprefix("rec-") for entry in listed if _kind(entry) == "staging"}
    )
    guard = _media_guard(store_dir) if keys and store is not None else _no_guard()
    with guard as guarded:
        # Read BEFORE the list lock is taken (no database access while holding it); for media,
        # while the maintenance lock keeps every media import out until the files are dealt with.
        facts = None
        if store is not None and ((keys and guarded) or sessions):
            facts = _facts(store, keys if guarded else [], sessions)
        referenced: set[str] = set()
        unreferenced: set[str] = set()
        finished: set[str] = set()
        if facts is not None:
            if guarded:
                referenced, unreferenced = facts[0], set(keys) - facts[0]
            finished = set(sessions) - facts[1]
        with _locked(store_dir, timeout=0) as locked:
            if not locked:
                return  # another change is in progress: a later sweep does this
            _drop_dead_temps(store_dir)
            entries, dropped = _load(store_dir)
            if dropped:
                LOG.warning("%d lines that are not entries were dropped from the list", dropped)
            remaining: list[str] = []
            for entry in entries:
                kind = _kind(entry)
                path = _resolve(store_dir, entry)
                if path is None:
                    continue
                if kind == "media":
                    key = entry.removeprefix("media/")
                    if key in referenced:
                        continue  # referenced again by a re-import: live data, not ours
                    if key not in unreferenced:
                        remaining.append(entry)  # not known to be unreferenced now: keep it
                        continue
                elif kind == "staging" and entry.split("/")[1].removeprefix("rec-") not in finished:
                    remaining.append(entry)  # its session is open, or that is unknown now
                    continue
                removed = _unlink(path)
                if removed is False:
                    remaining.append(entry)
                elif removed and kind == "staging":
                    with contextlib.suppress(OSError):
                        path.parent.rmdir()  # the session folder, once it is empty
            _write(store_dir, remaining)


def _retained(
    store: ProjectStore, digests: set[str], prefixes: set[str], *, wait: bool = False
) -> tuple[set[str], set[str]] | None:
    """Which of these digests, and which of these digest prefixes, the store still retains
    (inline or as media); ``None`` if that cannot be read (at once, unless ``wait``)."""
    try:
        conn = store.connect(readonly=True, wait=wait)
    except Exception:  # busy, unreadable, not openable: not now
        return None
    try:
        kept: set[str] = set()
        if digests:
            wanted = sorted(digests)
            marks = ",".join("?" for _ in wanted)
            rows = conn.execute(
                f"SELECT sha256 FROM payloads WHERE sha256 IN ({marks}) "  # noqa: S608
                f"UNION SELECT sha256 FROM media WHERE sha256 IN ({marks})",
                wanted + wanted,
            ).fetchall()
            kept = {str(row[0]) for row in rows}
        kept_prefixes: set[str] = set()
        for prefix in sorted(prefixes):
            # Lowercase hex digests that start with ``prefix`` sort in [prefix, prefix + "g").
            bounds = (prefix, prefix + "g")
            hit = conn.execute(
                "SELECT 1 FROM payloads WHERE sha256 >= ? AND sha256 < ? "
                "UNION ALL SELECT 1 FROM media WHERE sha256 >= ? AND sha256 < ? LIMIT 1",
                bounds + bounds,
            ).fetchone()
            if hit is not None:
                kept_prefixes.add(prefix)
        return kept, kept_prefixes
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def collect_unretained(store_dir: Path, store: ProjectStore) -> int:
    """Remove derived files in ``cache/`` whose recording the store no longer retains.

    A derived file exists because the recording's bytes are retained; once they are not (a
    purge removed them) it has no right to exist. This is the backstop under every other
    mechanism: whatever way such a file came to be there (a writer killed between writing and
    checking, a copy made after a purge cleared the cache), the next sweep with the store
    removes it. A file that cannot be removed now is listed. Returns how many were collected.
    Only derived names are considered, and the database is only read when there are some (at
    once: a busy store is left for a later sweep)."""
    try:
        with os.scandir(store_dir / "cache") as listing:
            names = [entry.name for entry in listing if entry.is_file()]
    except OSError:
        return 0
    entries, privates = _classify(names)
    if not entries and not privates:
        return 0
    found = _retained(store, set(entries.values()), set(privates.values()))
    if found is None:
        return 0
    kept, kept_prefixes = found
    doomed = [name for name, sha in entries.items() if sha not in kept]
    doomed += [name for name, prefix in privates.items() if prefix not in kept_prefixes]
    remove_or_list(store_dir, doomed)
    return len(doomed)
