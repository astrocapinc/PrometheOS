"""Family classes (A06.1, CP20.4; decision 0085): what kind of relation a family's files have, so
only real competition asks the owner.

Every family gets one class from deterministic rules, tried in this order:

1. ``identical``: every member has the same bytes.
2. ``tool_output``: every member lies under a generated folder (``slprj``, ``codegen``,
   ``slxdump``, ``_rels``, ``__pycache__``, a ``*_unpacked`` folder).
3. ``run_log``: every member is a log (``.log``, ``.out``, ``.diary``, or text under ``logs/``).
4. ``history``: exactly one member lies outside archive folders (``old``, ``archive``, ``backup``,
   ...); that one is the default.
5. ``succession``: all members share one folder and each name carries a distinct version or date
   token (``_v2``, ``_rev3``, ``_20260105``). The default is the ranked current when the ranking
   is decided or supported; otherwise the succession is undecided.
6. ``variant`` (``lineage`` for models): no two members share a folder, and the paths are equal
   except in exactly one folder at the same depth whose names read as parallel contexts: one
   shared prefix with a short numbered or lettered suffix (``track2/x.m``, ``track5/x.m``;
   ``L1``, ``L2``). Each member is current in its own context.
7. ``compete``: anything else.

The rules fail toward ``compete``: a family that fits no rule exactly asks the owner. Only
``compete`` and an undecided ``succession`` file an owner work item; every other class needs
none. A model's or an agent's opinion never sets a class; it can only be a proposal beside it.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Any

from prometheos.adoption.analyzers import name_stem
from prometheos.adoption.contracts import MEMBER_SEPARATOR

CLASSES = (
    "identical",
    "tool_output",
    "run_log",
    "history",
    "succession",
    "variant",
    "lineage",
    "compete",
)
GENERATED_FOLDERS = frozenset({"slprj", "slxdump", "_rels", "__pycache__"})
ARCHIVE_FOLDERS = frozenset(
    {"old", "archive", "archived", "backup", "backups", "bak", "legacy", "deprecated", "attic"}
)
# The only words a parallel-context folder name may be built on (review cp20-4-a-r2 R2-F1: a
# deny-list of version words always misses one, so the rule allows named contexts instead).
# ``v``, ``config`` and ``cfg`` are left out on purpose: V1/V2 and config1/config2 may be versions,
# so they compete (review cp20-4-a-r2, round 3).
CONTEXT_WORDS = frozenset(
    {
        "track", "case", "scenario", "level", "lvl", "l", "cell", "site", "lab", "vehicle",
        "veh", "condition", "regime",
    }
)  # fmt: skip
LOG_EXTENSIONS = frozenset({".log", ".out", ".diary"})
LOG_FOLDERS = frozenset({"log", "logs"})
# A token that orders versions: v3, rev2, r12, 2, or a date such as 20260105 or 2026-01-05.
_ORDER = re.compile(r"^(?:v|rev|r)?\d+(?:[-_.]\d+)*$", re.IGNORECASE)


def _parts(path: str) -> list[str]:
    """Folder segments (lower case) and the file name, through archive members."""
    flat = path.replace(MEMBER_SEPARATOR, "/")
    return [p for p in flat.split("/") if p]


def _folders(path: str) -> list[str]:
    return [p.lower() for p in _parts(path)[:-1]]


def _is_archive(folder: str) -> bool:
    """An archive folder: an archive word alone or followed by a number or a separator
    (``old``, ``old2``, ``backup_2024``; review cp20-4-a F1)."""
    word = re.match(r"[a-z]+", folder)
    return word is not None and word.group(0) in ARCHIVE_FOLDERS


def _order_token(path: str) -> str | None:
    tokens = name_stem(path)[1]
    found = [t for t in tokens if _ORDER.match(t)]
    return found[0] if found else None


def _one_segment_apart(paths: list[str]) -> bool:
    """Equal paths except one folder at the same depth, each in its own folder."""
    split = [_parts(p) for p in paths]
    if len({len(s) for s in split}) != 1 or len({s[-1].lower() for s in split}) != 1:
        return False
    if len({"/".join(s[:-1]).lower() for s in split}) != len(split):
        return False  # two members share a folder
    differing = [i for i in range(len(split[0]) - 1) if len({s[i].lower() for s in split}) > 1]
    if len(differing) != 1:
        return False
    return _contexts([s[differing[0]] for s in split])


def _contexts(values: list[str]) -> bool:
    """Folder names that read as parallel contexts: one shared prefix ending in a context word
    (``CONTEXT_WORDS``) with a short numbered or lettered suffix (``track2``/``track5``,
    ``L1``/``L2``, ``case_a``/``case_b``), none of them an archive folder. Anything else
    (``src``/``lib``, ``bus``/``van``, ``v1``/``v2``, ``build1``/``build2``) competes."""
    lowered = [v.lower() for v in values]
    if any(_is_archive(v) for v in lowered):
        return False
    prefix = os.path.commonprefix(lowered)
    if not any(c.isalpha() for c in prefix):
        return False
    words = re.findall(r"[a-z]+", prefix)
    if not words or words[-1] not in CONTEXT_WORDS:
        return False  # v1/v2, build1/build2, model_v2/model_v3: versions or unknown, so compete
    suffixes = [v[len(prefix) :] for v in lowered]
    return all(
        0 < len(x) <= 4 and (any(c.isdigit() for c in x) or (len(x) == 1 and x.isalpha()))
        for x in suffixes
    )


def classify(family: dict[str, Any], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The class of one family (as stored or shown), with its reason, the default member where
    the class has one, and whether it files an owner work item."""
    paths = [m["path"] for m in family["members"]]

    def result(cls: str, reason: str, default: str | None = None) -> dict[str, Any]:
        # Review cp20-4-a F2 and r2 R2-F2: a ranked current that is not the class's default asks
        # the owner even when the ranking looks settled.
        off_default = default is not None and family.get("current") not in (None, default)
        needs_owner = (
            cls == "compete"
            or (cls == "succession" and default is None)
            or bool(family.get("conflicts"))
            or off_default
        )
        return {
            "class": cls,
            "reason": reason,
            "default": default,
            "files_item": needs_owner,
            "off_default": off_default,
        }

    digests = [(rows.get(p) or {}).get("sha256") for p in paths]
    if all(digests) and len(set(digests)) == 1:
        return result("identical", "every member has the same bytes")
    if all(any(f in GENERATED_FOLDERS for f in _folders(p)) for p in paths):
        return result("tool_output", "every member lies under a generated folder")
    if all(
        PurePosixPath(p).suffix.lower() in LOG_EXTENSIONS
        or (PurePosixPath(p).suffix.lower() == ".txt" and set(_folders(p)) & LOG_FOLDERS)
        for p in paths
    ):
        return result("run_log", "every member is a run log")
    live = [p for p in paths if not any(_is_archive(f) for f in _folders(p))]
    if len(live) == 1 and len(paths) > 1:
        return result("history", "one live file; the others lie in archive folders", live[0])
    tokens = [_order_token(p) for p in paths]
    if (
        len({"/".join(_folders(p)) for p in paths}) == 1
        and all(tokens)
        and len(set(tokens)) == len(tokens)
    ):
        default = family.get("current") if family.get("band") in ("decided", "supported") else None
        reason = "numbered versions in one folder"
        return result("succession", reason, default)
    if _one_segment_apart(paths):
        if family.get("kind") == "model":
            return result("lineage", "the same model carried through one folder level")
        return result("variant", "the same file in parallel folders; each is current in its own")
    return result("compete", "different files where one should be current")


__all__ = ["CLASSES", "classify"]
