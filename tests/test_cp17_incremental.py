"""C17-INCREMENTAL (A05, CP17.6; decision 0045): re-adoption records generations and detects
added, changed, moved and deleted files and drift from the accepted baseline, re-analysing only
what changed; vault import brings a notes vault's Markdown in as sources without writing to it;
the baseline exports (a package and a one-way fenced Markdown note) and re-imports into a fresh
store, where the package fused with its own decisions must give the families the baseline
accepted (members and current files with digests, bands). Synthetic data only; no model, no
network."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from adoption_corpus import CORPUS_LIMITS, generate
from cp1_support import AGENT, OWNER, OWNER_ACT, Env
from test_cp10_authority import grant, refused
from test_cp17_inventory import _add_root, placeholder_provider, placeholders_of, tree_digest

from prometheos.adoption import drift, transfer
from prometheos.adoption import scan as scan_mod

SEED = 17


def _scan(env: Env, alias: str, provider: Any) -> dict[str, Any]:
    original = scan_mod.STAT_PROVIDER
    scan_mod.STAT_PROVIDER = provider
    try:
        scan = env.svc.adoption_scan(
            env.project_id, AGENT, root_alias=alias, limits=CORPUS_LIMITS, mode="wait"
        )
    finally:
        scan_mod.STAT_PROVIDER = original
    assert scan["state"] == "complete", scan
    return scan


def _families(env: Env, gen: int) -> list[tuple[str, tuple[str, ...], str | None, str]]:
    out = []
    for f in env.svc.adoption_families(env.project_id, OWNER, gen, limit=500)["families"]:
        family = env.svc.adoption_family(env.project_id, OWNER, gen, f["family_id"])
        out.append(
            (
                family["key"],
                tuple(sorted(m["path"] for m in family["members"])),
                family["current"],
                family["band"],
            )
        )
    return sorted(out)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    base = tmp_path_factory.mktemp("incremental")
    root = base / "corpus"
    golden = generate(root, SEED)
    (base / "env").mkdir()
    env = Env(base / "env")
    _add_root(env, "corpus", root)
    provider = placeholder_provider(root, placeholders_of(golden))
    first = _scan(env, "corpus", provider)
    gen = first["scan_gen"]
    family = next(
        f for f in env.svc.adoption_families(env.project_id, OWNER, gen)["families"]
        if f["key"] == "data:sweep"
    )  # fmt: skip
    detail = env.svc.adoption_family(env.project_id, OWNER, gen, family["family_id"])
    env.svc.adoption_decide(
        env.project_id, gen, family["family_id"], detail["current"], "checked",
        authority=OWNER_ACT,
    )  # fmt: skip
    baseline = env.svc.adoption_baseline_accept(env.project_id, gen, "first", authority=OWNER_ACT)
    notes = base / "notes-crlf"  # an approved notes folder that is not adopted
    notes.mkdir()
    _add_root(env, "notes-crlf", notes)
    return {
        "env": env,
        "root": root,
        "golden": golden,
        "provider": provider,
        "gen": gen,
        "baseline": baseline,
        "decided": (family["family_id"], detail["current"]),
        "base": base,
    }


# ----- re-adoption --------------------------------------------------------------------------


def test_a_rescan_classifies_every_change_and_reanalyses_only_those(world: dict[str, Any]) -> None:
    env, root = world["env"], world["root"]
    _family, decided_current = world["decided"]
    # change, move, delete and add one file each; move the decided current file too
    (root / "notes" / "decision-log.md").write_text(
        (root / "notes" / "decision-log.md").read_text("utf-8") + "\n- 2026-04-01: rerun later.\n",
        encoding="utf-8",
    )
    moved_to = "results/archive_moved/" + decided_current.rsplit("/", 1)[-1]
    (root / moved_to).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root / decided_current), str(root / moved_to))
    (root / "notes" / "Zusammenfassung_Résumé_ß.md").unlink()
    (root / "notes" / "new-idea.md").write_text("# A new idea\n\nTry a finer sweep.\n", "utf-8")
    second = _scan(env, "corpus", world["provider"])
    world["gen2"] = second["scan_gen"]
    view = env.svc.adoption_drift(env.project_id, OWNER, second["scan_gen"])
    assert view["against"] == {"kind": "previous", "scan_gen": world["gen"], "baseline_id": None}
    changes = {(e["change"], e["path"]) for e in view["entries"]}
    assert ("changed", "notes/decision-log.md") in changes
    assert ("moved", moved_to) in changes
    assert ("deleted", "notes/Zusammenfassung_Résumé_ß.md") in changes
    assert ("added", "notes/new-idea.md") in changes
    moved = next(e for e in view["entries"] if e["change"] == "moved")
    assert moved["from"] == decided_current and moved["sha256_before"] == moved["sha256_after"]
    assert view["counts"] == {"added": 1, "changed": 1, "moved": 1, "deleted": 1}, view["counts"]
    # Only what changed is analysed again: the two text files with new bytes are extracted,
    # everything else replays from the content-keyed cache.
    scans = {s["scan_gen"]: s for s in env.svc.adoption_scans(env.project_id, OWNER)["scans"]}
    first_work = scans[world["gen"]]["analysis"]["extracted"]
    assert view["analysis"]["replayed"] >= first_work - 3 > 0
    assert view["analysis"]["extracted"] <= 3
    # The owner's decision follows the moved file (same digest): still R1, now at its new path.
    # (The family's ID can change with its smallest member; the decision follows the file.)
    family = next(
        env.svc.adoption_family(env.project_id, OWNER, second["scan_gen"], f["family_id"])
        for f in env.svc.adoption_families(env.project_id, OWNER, second["scan_gen"])["families"]
        if f["current"] == moved_to
    )
    assert family["current"] == moved_to and family["deciding_rule"] == "R1"


def test_drift_from_the_baseline_says_what_became_of_each_current_file(
    world: dict[str, Any],
) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    view = env.svc.adoption_drift(env.project_id, OWNER, world["gen2"], against=baseline_id)
    assert view["against"]["kind"] == "baseline" and view["against"]["scan_gen"] == world["gen"]
    by_family = {f["family_id"]: f for f in view["families"]}
    decided_family, decided_current = world["decided"]
    assert by_family[decided_family]["change"] == "moved"
    assert by_family[decided_family]["now"].endswith(decided_current.rsplit("/", 1)[-1])
    assert {f["change"] for f in view["families"]} <= {
        "kept",
        "moved",
        "changed",
        "deleted",
        "undecided",
    }
    assert any(f["change"] == "kept" for f in view["families"])


def test_old_generations_are_pruned_but_baselines_kept(world: dict[str, Any]) -> None:
    env = world["env"]
    gens = [
        _scan(env, "corpus", world["provider"])["scan_gen"] for _ in range(drift.KEEP_GENERATIONS)
    ]
    scans = {
        s["scan_gen"]: s["state"] for s in env.svc.adoption_scans(env.project_id, OWNER)["scans"]
    }
    assert scans[world["gen"]] == "complete", "the baseline's generation is kept"
    assert scans[world["gen2"]] == "pruned"
    assert all(scans[g] == "complete" for g in gens)
    assert env.svc.adoption_files(env.project_id, OWNER, world["gen2"])["total"] == 0
    with pytest.raises(Exception) as err:
        env.svc.adoption_drift(env.project_id, OWNER, world["gen2"])
    assert getattr(err.value, "code", "") == "validation_error"


def test_a_scan_never_writes_into_the_folder(world: dict[str, Any]) -> None:
    before = tree_digest(world["root"])
    _scan(world["env"], "corpus", world["provider"])
    assert tree_digest(world["root"]) == before


# ----- package export and re-import -------------------------------------------------------


def test_the_baseline_reimports_into_a_fresh_store_with_equivalent_families(
    world: dict[str, Any],
) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    package = env.svc.adoption_baseline_export(env.project_id, AGENT, baseline_id)
    assert package["kind"] == "prometheos-adoption-baseline" and len(package["sha256"]) == 64
    (world["base"] / "fresh").mkdir()
    fresh = Env(world["base"] / "fresh")
    denied = refused(
        lambda: fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=None)
    )
    assert denied.code == "policy_denied", "an agent cannot import owner decisions"
    tampered = {
        **package,
        "decisions": [*package["decisions"], {**package["decisions"][0], "current_path": "x.mat"}],
    }
    bad = refused(
        lambda: fresh.svc.adoption_baseline_import(fresh.project_id, tampered, authority=OWNER_ACT)
    )
    assert bad.code == "validation_error" and "digest" in bad.message
    done = fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    assert done["baseline_id"] == baseline_id and done["decisions"] == len(package["decisions"])
    assert _families(fresh, done["scan_gen"]) == _families(env, world["gen"])
    again = refused(
        lambda: fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    )
    assert again.code == "validation_error"
    copy = fresh.svc.adoption_baseline(fresh.project_id, OWNER, baseline_id)
    assert copy["sha256"] == world["baseline"]["sha256"]


# ----- the one-way fenced Markdown note and vault import -----------------------------------


def test_the_markdown_note_keeps_the_owners_text_and_is_never_read_back(
    world: dict[str, Any],
) -> None:
    env = world["env"]
    vault = world["base"] / "vault"
    (vault / "PrometheOS").mkdir(parents=True)
    (vault / "Daily.md").write_text("# Daily\n\nSee results/sweep_v1.mat.\n", "utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.md").write_text("internal\n", "utf-8")
    (vault / "attachments").mkdir()
    (vault / "attachments" / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _add_root(env, "vault", vault)
    baseline_id = world["baseline"]["baseline_id"]
    asked = refused(
        lambda: env.svc.approval_request(
            env.project_id, AGENT, "adoption_export_markdown",
            {"baseline_id": baseline_id, "root_alias": "vault", "folder": "PrometheOS"},
        )
    )  # fmt: skip
    assert asked.code == "approval_required"
    assert not (vault / "PrometheOS" / "adoption-corpus.md").exists(), "nothing before the grant"
    grant(env, asked.details["approval_request_id"])
    done = env.svc.approval_resume(env.project_id, AGENT, asked.details["approval_request_id"])
    note = vault / "PrometheOS" / "adoption-corpus.md"
    assert done["result"]["note"] == "PrometheOS/adoption-corpus.md" and note.exists()
    first = note.read_text("utf-8")
    assert first.startswith(transfer.FENCE_BEGIN) and "| data:sweep |" in first
    # The owner writes around the block, and edits inside it; a new export keeps the former.
    edited = "My own heading\n\n" + first.replace("| data:sweep |", "| HAND EDIT |") + "\nAfter.\n"
    note.write_text(edited, encoding="utf-8")
    env.svc.adoption_export_markdown(
        env.project_id, baseline_id, "vault", "PrometheOS", authority=OWNER_ACT
    )
    second = note.read_text("utf-8")
    assert second.startswith("My own heading\n\n") and second.endswith("\nAfter.\n")
    assert "HAND EDIT" not in second and "| data:sweep |" in second
    assert second.count(transfer.FENCE_BEGIN) == 1
    # Vault import: notes only, housekeeping folders and PrometheOS's own export left out,
    # nothing written into the vault.
    before = tree_digest(vault)
    planned = env.svc.adoption_vault_import(env.project_id, AGENT, "vault")
    assert planned["paths"] == ["Daily.md"]
    assert planned["skipped"]["exported_by_prometheos"] == 1
    plan_id = planned["import"]["plan_id"]
    committed = env.svc.import_commit(env.project_id, plan_id, AGENT)
    sources = [r["id"] for r in committed["results"] if r["id"].startswith("source_")]
    assert len(sources) == 1, "one note, one source"
    assert tree_digest(vault) == before


# ----- review-precheck-cp17-6-a F1-F4 -----------------------------------------------------------


def test_the_note_keeps_crlf_and_bom_and_refuses_other_encodings(world: dict[str, Any]) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    notes = world["base"] / "notes-crlf"  # approved by the fixture
    note = notes / "adoption-corpus.md"
    mine = b"\xef\xbb\xbfMy heading\r\n\r\nMy own line.\r\n"
    note.write_bytes(mine)
    env.svc.adoption_export_markdown(
        env.project_id, baseline_id, "notes-crlf", "", authority=OWNER_ACT
    )
    data = note.read_bytes()
    assert data.startswith(mine), "the owner's bytes, BOM and CRLF included, are kept"
    assert data.count(b"\n") == data.count(b"\r\n"), "the block follows the note's line endings"
    latin = notes / "latin"
    latin.mkdir()
    (latin / "adoption-corpus.md").write_bytes(b"Caf\xe9 notes\n")
    bad = refused(
        lambda: env.svc.adoption_export_markdown(
            env.project_id, baseline_id, "notes-crlf", "latin", authority=OWNER_ACT
        )
    )
    assert bad.code == "validation_error" and "UTF-8" in bad.message
    assert (latin / "adoption-corpus.md").read_bytes() == b"Caf\xe9 notes\n"


def test_the_note_is_never_written_into_an_adopted_folder(world: dict[str, Any]) -> None:
    env = world["env"]
    before = tree_digest(world["root"])
    denied = refused(
        lambda: env.svc.adoption_export_markdown(
            env.project_id, world["baseline"]["baseline_id"], "corpus", "notes", authority=OWNER_ACT
        )
    )
    assert denied.code == "policy_denied" and "adopts" in denied.message
    assert tree_digest(world["root"]) == before


def test_a_package_holds_the_decisions_of_its_baseline_only(world: dict[str, Any]) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    gen = env.svc.adoption_scans(env.project_id, OWNER)["scans"][0]["scan_gen"]
    later = next(
        f
        for f in env.svc.adoption_families(env.project_id, OWNER, gen, limit=500)["families"]
        if f["key"] != "data:sweep"
    )
    detail = env.svc.adoption_family(env.project_id, OWNER, gen, later["family_id"])
    env.svc.adoption_decide(
        env.project_id, gen, later["family_id"], detail["members"][-1]["path"], authority=OWNER_ACT
    )
    package = env.svc.adoption_baseline_export(env.project_id, AGENT, baseline_id)
    assert len(package["decisions"]) == 1, "the decision made after the baseline is not in it"
    (world["base"] / "fresh-2").mkdir()
    fresh = Env(world["base"] / "fresh-2")
    done = fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    imported = {
        (f["key"], f["current"], f["band"])
        for f in fresh.svc.adoption_families(fresh.project_id, OWNER, done["scan_gen"], limit=500)[
            "families"
        ]
    }
    accepted = {
        (f["key"], f["current"], f["band"]) for f in package["baseline"]["payload"]["families"]
    }
    assert imported == accepted, "the re-import shows the families the baseline accepted"


# ----- review-precheck-cp17-6-a-r2 R2-1: the write guards, each tested ------------------------


def test_the_note_cannot_leave_its_folder(world: dict[str, Any]) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    for folder in ("../outside", "notes/../../outside", "C:/Windows", "\\\\server\\share"):
        out = refused(
            lambda folder=folder: env.svc.adoption_export_markdown(
                env.project_id, baseline_id, "notes-crlf", folder, authority=OWNER_ACT
            )
        )
        assert out.code in ("validation_error", "policy_denied"), (folder, out.code)
    assert not (world["base"] / "outside").exists()


def test_a_note_changed_after_the_request_is_not_overwritten(world: dict[str, Any]) -> None:
    env = world["env"]
    params = {"baseline_id": world["baseline"]["baseline_id"], "root_alias": "notes-crlf",
              "folder": "stale"}  # fmt: skip
    asked = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "adoption_export_markdown", params)
    )
    approval_id = asked.details["approval_request_id"]
    grant(env, approval_id)
    note = world["base"] / "notes-crlf" / "stale" / "adoption-corpus.md"
    note.parent.mkdir(parents=True)
    note.write_text("Written by the owner after the request.\n", "utf-8")
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    assert out.code == "revision_conflict"
    assert note.read_text("utf-8") == "Written by the owner after the request.\n"


@pytest.mark.parametrize(
    "existing",
    [
        "{block}\nmine\n{block}",  # the block twice
        "<!-- prometheos:begin adoption root=corpus baseline=x -->\nno end marker\n",  # damaged
    ],
    ids=["doubled", "damaged"],
)
def test_a_doubled_or_damaged_block_is_refused(world: dict[str, Any], existing: str) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    folder = world["base"] / "notes-crlf" / ("fence-" + str(abs(hash(existing)) % 10_000))
    folder.mkdir(parents=True)
    block = transfer.FENCE_BEGIN + " root=corpus baseline=x -->\nold\n" + transfer.FENCE_END
    block += " root=corpus -->"
    note = folder / "adoption-corpus.md"
    note.write_text(existing.format(block=block), "utf-8")
    before = note.read_bytes()
    out = refused(
        lambda: env.svc.adoption_export_markdown(
            env.project_id, baseline_id, "notes-crlf", folder.name, authority=OWNER_ACT
        )
    )
    assert out.code == "validation_error"
    assert note.read_bytes() == before


def _row(path: str, sha: str) -> Any:
    from prometheos.adoption.contracts import AdoptionFile

    # compare reads only path, entry, status and sha256
    return AdoptionFile.model_construct(path=path, entry="file", status="read", sha256=sha * 64)


def test_a_rename_onto_an_existing_path_is_a_change_and_a_deletion() -> None:
    older = [_row("a.md", "a"), _row("b.md", "b")]
    newer = [_row("b.md", "a")]  # a.md renamed over b.md
    changes = {(e["change"], e["path"]) for e in drift.compare(older, newer)}
    assert changes == {("changed", "b.md"), ("deleted", "a.md")}


# ----- review-precheck-cp17-6-a-r3 R3-1, R3-2 -------------------------------------------------


def test_a_junction_on_the_way_is_refused(world: dict[str, Any]) -> None:
    _winapi = pytest.importorskip("_winapi")
    env = world["env"]
    # pointing inside the approved folder, so only the link guard (not the escape check) refuses
    outside = world["base"] / "notes-crlf" / "real-target"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(world["base"] / "notes-crlf" / "jx"))
    out = refused(
        lambda: env.svc.adoption_export_markdown(
            env.project_id, world["baseline"]["baseline_id"], "notes-crlf", "jx",
            authority=OWNER_ACT,
        )
    )  # fmt: skip
    assert out.code == "policy_denied"
    assert list(outside.iterdir()) == []


def test_a_placeholder_note_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each placeholder guard on its own (review cp17-6-a-r4 R4-2): reading refuses before any
    byte is read, and writing refuses even when the caller already holds the old text."""
    from prometheos.adoption.inventory import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    from prometheos.domain.models import PrometheosError

    note = tmp_path / "adoption-corpus.md"
    note.write_text("in the cloud\n", "utf-8")
    monkeypatch.setattr(transfer, "_attributes", lambda path: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    monkeypatch.setattr(transfer, "_decoded", lambda data: pytest.fail("the note was read"))
    # Opening the note at all, by any route, fails the test: a read placed before the attribute
    # check would download a cloud placeholder (review cp17-6-a-r5 R5-2).
    import builtins
    import io
    import pathlib

    def never(real: Any) -> Any:
        def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
            if str(file).replace("\\", "/").endswith("/" + note.name):
                pytest.fail("a byte of the placeholder note was read")
            return real(file, *args, **kwargs)

        return guarded

    monkeypatch.setattr(builtins, "open", never(builtins.open))
    monkeypatch.setattr(io, "open", never(io.open))
    monkeypatch.setattr(os, "open", never(os.open))
    for name in ("read_bytes", "read_text", "open"):
        monkeypatch.setattr(pathlib.Path, name, never(getattr(pathlib.Path, name)))
    with pytest.raises(PrometheosError) as err:
        transfer.read_note(tmp_path, note.name)
    assert err.value.code == "policy_denied"
    with pytest.raises(PrometheosError) as err:
        transfer.write_note(tmp_path, tmp_path, note.name, "new\n", "in the cloud\n")
    assert err.value.code == "policy_denied"
    monkeypatch.undo()
    assert note.read_text("utf-8") == "in the cloud\n"


def test_a_crafted_package_is_refused_and_genuine_ones_import_in_any_order(
    world: dict[str, Any],
) -> None:
    from prometheos.domain.canonical import canonical_sha256

    env = world["env"]
    first = env.svc.adoption_baseline_export(
        env.project_id, AGENT, world["baseline"]["baseline_id"]
    )
    gen = env.svc.adoption_scans(env.project_id, OWNER)["scans"][0]["scan_gen"]
    second_id = env.svc.adoption_baseline_accept(env.project_id, gen, authority=OWNER_ACT)[
        "baseline_id"
    ]
    second = env.svc.adoption_baseline_export(env.project_id, AGENT, second_id)
    # a package whose payload claims other families, with both digests recomputed
    crafted = json_copy(first)
    family = crafted["baseline"]["payload"]["families"][0]
    family["band"] = "decided" if family["band"] != "decided" else "weak"
    crafted["baseline"]["sha256"] = canonical_sha256(crafted["baseline"]["payload"])
    crafted["sha256"] = canonical_sha256({k: v for k, v in crafted.items() if k != "sha256"})
    (world["base"] / "fresh-3").mkdir()
    fresh = Env(world["base"] / "fresh-3")
    out = refused(
        lambda: fresh.svc.adoption_baseline_import(fresh.project_id, crafted, authority=OWNER_ACT)
    )
    assert out.code == "validation_error" and "reproduce" in out.message
    assert fresh.svc.adoption_scans(fresh.project_id, OWNER)["scans"] == [], "nothing kept"
    # genuine packages: newer then older, into a store that holds its own decisions by then
    fresh.svc.adoption_baseline_import(fresh.project_id, second, authority=OWNER_ACT)
    fresh.svc.adoption_baseline_import(fresh.project_id, first, authority=OWNER_ACT)
    assert len(fresh.svc.adoption_baselines(fresh.project_id, OWNER)["baselines"]) == 2


def json_copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value))


def test_an_unranked_baseline_round_trips_and_cannot_carry_families(tmp_path: Path) -> None:
    """Review cp17-6-a-r4 R4-1: a scan made without ranking (the dogfood mode) has a baseline
    without families; its package imports, and one that claims families is refused."""
    from prometheos.domain.canonical import canonical_sha256

    root = tmp_path / "tree"
    (root / "results").mkdir(parents=True)
    for v in (1, 2):
        (root / "results" / f"sweep_v{v}.csv").write_text(f"a,b\n{v},2\n", "utf-8")
    (tmp_path / "env").mkdir()
    env = Env(tmp_path / "env")
    _add_root(env, "tree", root)
    scan = env.svc.adoption_scan(env.project_id, AGENT, root_alias="tree", rank=False, mode="wait")
    bl = env.svc.adoption_baseline_accept(env.project_id, scan["scan_gen"], authority=OWNER_ACT)
    package = env.svc.adoption_baseline_export(env.project_id, AGENT, bl["baseline_id"])
    assert package["baseline"]["payload"]["families"] == []
    (tmp_path / "fresh").mkdir()
    fresh = Env(tmp_path / "fresh")
    done = fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    assert done["families"] == 0
    crafted = json_copy(package)
    crafted["baseline"]["payload"]["families"] = [
        {"family_id": "fam_" + "0" * 16, "key": "data:sweep", "band": "decided",
         "current": "results/sweep_v2.csv", "current_sha256": None, "members": []}
    ]  # fmt: skip
    crafted["baseline"]["baseline_id"] = "bl_" + "1" * 24
    crafted["baseline"]["sha256"] = canonical_sha256(crafted["baseline"]["payload"])
    crafted["sha256"] = canonical_sha256({k: v for k, v in crafted.items() if k != "sha256"})
    (tmp_path / "fresh-2").mkdir()
    other = Env(tmp_path / "fresh-2")
    out = refused(
        lambda: other.svc.adoption_baseline_import(other.project_id, crafted, authority=OWNER_ACT)
    )
    assert out.code == "validation_error"
    assert other.svc.adoption_scans(other.project_id, OWNER)["scans"] == []


@pytest.mark.parametrize("field", ["member_digest", "current_sha256", "member_path", "root_alias"])
def test_a_package_with_forged_digests_is_refused(world: dict[str, Any], field: str) -> None:
    """Review cp17-6-a-r5 R5-1: members' digests, the current file's digest, the members
    themselves and the baseline's folder are compared."""
    from prometheos.domain.canonical import canonical_sha256

    env = world["env"]
    package = json_copy(
        env.svc.adoption_baseline_export(env.project_id, AGENT, world["baseline"]["baseline_id"])
    )
    payload = package["baseline"]["payload"]
    family = next(f for f in payload["families"] if f["current"])
    if field == "member_digest":
        family["members"][0][1] = "0" * 64
    elif field == "current_sha256":
        family["current_sha256"] = "0" * 64
    elif field == "member_path":
        # another inventory file, with its true digest: only the members comparison sees it
        members = {p for f in payload["families"] for p, _sha in f["members"]}
        other = next(r for r in package["files"] if r["sha256"] and r["path"] not in members)
        family["members"][-1] = [other["path"], other["sha256"]]
    else:
        payload["root_alias"] = "elsewhere"
    package["baseline"]["sha256"] = canonical_sha256(package["baseline"]["payload"])
    package["sha256"] = canonical_sha256({k: v for k, v in package.items() if k != "sha256"})
    (world["base"] / f"fresh-forged-{field}").mkdir()
    fresh = Env(world["base"] / f"fresh-forged-{field}")
    out = refused(
        lambda: fresh.svc.adoption_baseline_import(fresh.project_id, package, authority=OWNER_ACT)
    )
    assert out.code == "validation_error"
    assert fresh.svc.adoption_scans(fresh.project_id, OWNER)["scans"] == []


# ----- review-precheck-cp17-6-a-r3 R3-1 (4): the cases section 10 listed as untested ------------


def test_a_note_with_mixed_line_endings_survives_a_second_export(world: dict[str, Any]) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    folder = world["base"] / "notes-crlf" / "mixed"
    folder.mkdir()
    note = folder / "adoption-corpus.md"
    mine = b"Top line\nA Windows line\r\nLast line\n"
    note.write_bytes(mine)
    for _ in range(2):
        env.svc.adoption_export_markdown(
            env.project_id, baseline_id, "notes-crlf", "mixed", authority=OWNER_ACT
        )
        data = note.read_bytes()
        assert data.startswith(mine), "the owner's bytes are kept, line endings included"
        assert data.count(transfer.FENCE_BEGIN.encode()) == 1, "replaced, not appended again"
        block = data[len(mine) :]
        assert block.count(b"\n") == block.count(b"\r\n"), "a note with CRLF gets a CRLF block"
    again = env.svc.adoption_export_markdown_plan(
        env.project_id, AGENT, baseline_id, "notes-crlf", "mixed"
    )
    assert again["action"] == "unchanged", "a third export would change nothing"


def test_drift_pairs_files_of_equal_content_one_to_one() -> None:
    older = [_row("a/x.csv", "d"), _row("b/x.csv", "d"), _row("c.csv", "e")]
    newer = [_row("n1/x.csv", "d"), _row("n2/x.csv", "d"), _row("n3/x.csv", "d")]
    changes = drift.compare(older, newer)
    assert changes == drift.compare(older, newer), "the pairing is deterministic"
    moved = [e for e in changes if e["change"] == "moved"]
    assert sorted(e["from"] for e in moved) == ["a/x.csv", "b/x.csv"], "each source moves once"
    assert [(e["change"], e["path"]) for e in changes if e["change"] != "moved"] == [
        ("added", "n3/x.csv"),
        ("deleted", "c.csv"),
    ]
    assert sorted(e["path"] for e in changes) == ["c.csv", "n1/x.csv", "n2/x.csv", "n3/x.csv"]


def _requested(env: Env, folder: str, baseline_id: str) -> str:
    params = {"baseline_id": baseline_id, "root_alias": "notes-crlf", "folder": folder}
    asked = refused(
        lambda: env.svc.approval_request(env.project_id, AGENT, "adoption_export_markdown", params)
    )
    assert asked.code == "approval_required"
    approval_id = str(asked.details["approval_request_id"])
    grant(env, approval_id)
    return approval_id


def test_a_second_resume_replays_and_writes_nothing(world: dict[str, Any]) -> None:
    env = world["env"]
    approval_id = _requested(env, "replay", world["baseline"]["baseline_id"])
    first = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert first["replayed"] is False
    note = world["base"] / "notes-crlf" / "replay" / "adoption-corpus.md"
    # an edit inside the block: a second write would replace it
    note.write_text(note.read_text("utf-8").replace("| data:sweep |", "| HAND EDIT |"), "utf-8")
    before = note.read_bytes()
    again = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert again["replayed"] is True and again["result"] == first["result"]
    assert note.read_bytes() == before


def test_a_write_that_fails_after_the_grant_is_spent_needs_a_new_request(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = world["env"]
    baseline_id = world["baseline"]["baseline_id"]
    approval_id = _requested(env, "spent", baseline_id)
    note = world["base"] / "notes-crlf" / "spent" / "adoption-corpus.md"
    real = transfer.write_note

    def racing(folder: Path, root: Path, name: str, text: str, previous: str | None) -> str:
        # the owner saves the note after the grant was spent and before the write
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("Owner wrote this meanwhile.\n", "utf-8")
        return real(folder, root, name, text, previous)

    monkeypatch.setattr(transfer, "write_note", racing)
    out = refused(lambda: env.svc.approval_resume(env.project_id, AGENT, approval_id))
    monkeypatch.undo()
    assert out.code == "revision_conflict"
    assert note.read_text("utf-8") == "Owner wrote this meanwhile.\n"
    again = env.svc.approval_resume(env.project_id, AGENT, approval_id)
    assert again["replayed"] is True and "revision_conflict" in json.dumps(again["result"])
    assert note.read_text("utf-8") == "Owner wrote this meanwhile.\n", "nothing written on replay"
    fresh_id = _requested(env, "spent", baseline_id)
    assert fresh_id != approval_id
    done = env.svc.approval_resume(env.project_id, AGENT, fresh_id)
    assert done["replayed"] is False
    assert note.read_text("utf-8").startswith("Owner wrote this meanwhile.\n")
    assert note.read_text("utf-8").count(transfer.FENCE_BEGIN) == 1
