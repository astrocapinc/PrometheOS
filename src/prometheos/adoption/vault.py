"""Vault import: an existing notes vault's Markdown notes as sources (A05, CP17.6;
C17-INCREMENTAL; decision 0045).

The vault is an approved research root. Its notes are selected by the same walk the inventory
uses (links not followed, cloud placeholders and placeholder folders never opened), the
vault's own housekeeping folders (``.obsidian``, ``.trash``, ``.git`` ...) and notes that carry a
PrometheOS export fence are left out (the export is one-way), and the selection goes through
the ordinary source import: a plan the agent or the owner makes, then a commit, in batches of
at most 100 notes. Nothing is written into the vault.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prometheos.adoption import inventory as inv
from prometheos.adoption.transfer import FENCE_BEGIN

NOTE_SUFFIXES = (".md", ".markdown")
VAULT_FOLDERS = frozenset({".obsidian", ".trash", ".git", ".logseq", ".foam", ".vscode"})
MAX_NOTE_BYTES = 4 * 1024 * 1024
BATCH = 100


def select(
    root: Path, stat_of: inv.StatProvider = inv.entry_stat, *, offset: int = 0
) -> dict[str, Any]:
    """The vault's notes in path order, one batch from ``offset``, with what was left out."""

    def prune(rel: str, st: inv.Stat) -> str | None:
        if rel.rsplit("/", 1)[-1].lower() in VAULT_FOLDERS:
            return "excluded"
        return None

    notes: list[str] = []
    skipped = {"placeholder": 0, "too_large": 0, "exported_by_prometheos": 0, "unreadable": 0}
    for walked in inv.walk(root, stat_of, prune):
        st = walked.stat
        if walked.error or walked.pruned or st is None or st.kind != "file":
            if walked.pruned == "placeholder":
                skipped["placeholder"] += 1
            continue
        if not walked.rel.lower().endswith(NOTE_SUFFIXES):
            continue
        if inv.is_placeholder(st):
            skipped["placeholder"] += 1
            continue
        if st.size > MAX_NOTE_BYTES:
            skipped["too_large"] += 1
            continue
        try:
            with open(inv.os_path(root / walked.rel), "rb") as handle:
                head = handle.read(MAX_NOTE_BYTES)
        except OSError:
            skipped["unreadable"] += 1
            continue
        if FENCE_BEGIN.encode() in head:
            skipped["exported_by_prometheos"] += 1
            continue
        notes.append(walked.rel)
    notes.sort()
    batch = notes[offset : offset + BATCH]
    end = offset + len(batch)
    return {
        "paths": batch,
        "total": len(notes),
        "offset": offset,
        "next_offset": end if end < len(notes) else None,
        "skipped": skipped,
    }
