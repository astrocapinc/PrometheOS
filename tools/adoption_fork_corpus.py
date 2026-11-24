"""A seeded synthetic research repository that forks by track, with golden family classes (CP20.4,
C20-ADOPT; decision 0085).

    uv run python tools/adoption_fork_corpus.py --seed 17 --out DIR [--golden FILE]

Writes the folder into ``DIR`` (which must not exist) and its golden labels as JSON beside it
(default ``DIR.golden.json``), never inside it. The same seed gives byte-identical output.

The shape follows a repository like the owner's: per-track forks of the same scripts, data copied
between tracks, a model lineage across levels, dated run logs, a versioned report, an archive of
old copies, generated exports, and real competition (two different files where one should be
current). Each family carries the class an owner would give it:

- ``variant``: the same file per track, each current in its own track;
- ``identical``: byte-identical copies;
- ``tool_output``: files under a generated folder;
- ``run_log``: dated logs of runs;
- ``history``: one live file and old copies in archive folders;
- ``lineage``: a model carried from level to level;
- ``succession``: numbered versions in one folder, the newest the default;
- ``compete``: different files where the owner must say which is current. Traps make some of them
  look like variants (one member one folder deeper, two members in one track folder).

Everything is synthetic; nothing is copied from a real project.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adoption_corpus import mdl_text

GENERATOR = "adoption-fork-corpus"
# 1.0.1: the succession family was named "summary_v*", a generic stem that fusion never groups by
# design, so it could not form (first measurement, development/impact/cp20-4/; decision 0085).
# 1.1.0: two more compete traps (version-named folders; an export beside a live file) after
# review cp20-4-a F1 and F3. Targets unchanged.
GENERATOR_VERSION = "1.1.0"
CLASSES = (
    "variant",
    "identical",
    "tool_output",
    "run_log",
    "history",
    "lineage",
    "succession",
    "compete",
)
TRACKS = ("track2", "track3", "track4", "track5", "track6", "track7", "track8", "track9")


def _script(rng: random.Random, name: str, extra: str = "") -> str:
    gain = rng.randint(10, 999) / 10
    steps = rng.randint(50, 900)
    return (
        f"% {name}: synthetic analysis script\n"
        f"gain = {gain};\n"
        f"steps = {steps};\n"
        f"result = gain * steps;  % {extra}\n"
        "disp(result);\n"
    )


def _csv(rng: random.Random, rows: int = 4) -> str:
    lines = ["t_s,speed_mps,power_kw"]
    for i in range(rows):
        lines.append(f"{i},{rng.randint(0, 30)}.{rng.randint(0, 9)},{rng.randint(-80, 80)}")
    return "\n".join(lines) + "\n"


def generate(root: Path, seed: int) -> dict[str, Any]:
    if root.exists():
        raise SystemExit(f"{root} already exists")
    rng = random.Random(seed)  # noqa: S311 - synthetic content, not security
    files: dict[str, str] = {}
    families: list[dict[str, Any]] = []

    def family(cls: str, members: dict[str, str], why: str) -> None:
        files.update(members)
        families.append({"class": cls, "members": sorted(members), "why": why})

    tracks = sorted(rng.sample(TRACKS, k=rng.randint(3, 5)))

    # variant: one script and one parameter table per track, each different.
    family(
        "variant",
        {f"{t}/smoke_ocp.m": _script(rng, "smoke_ocp", t) for t in tracks},
        "the same script forked per track; each is current in its track",
    )
    family(
        "variant",
        {f"{t}/data/vehicle_params.csv": _csv(rng) for t in tracks[:3]},
        "per-track parameter tables",
    )
    # identical: one drive cycle copied byte for byte.
    cycle = _csv(rng, rows=6)
    family(
        "identical",
        {
            "data/cycles/nrel_ftp.csv": cycle,
            **{f"{t}/cycles/nrel_ftp.csv": cycle for t in tracks[:2]},
        },
        "one drive cycle copied into tracks",
    )
    # tool_output: generated block-diagram dumps of two export runs.
    family(
        "tool_output",
        {
            f"exports/slxdump/{run}/blockdiagram.xml": (
                f"<blockdiagram run='{run}' n='{rng.randint(1, 99)}'/>\n"
            )
            for run in ("run1", "run2")
        },
        "generated exports",
    )
    # run_log: dated logs of the same simulation.
    days = sorted(rng.sample(range(5, 28), k=3))
    family(
        "run_log",
        {
            f"logs/sim_run_202601{d:02d}.log": (
                f"run 202601{d:02d}\nenergy_kj={rng.randint(400, 999)}\n"
            )
            for d in days
        },
        "dated run logs",
    )
    # history: one live script, old copies in archive folders.
    family(
        "history",
        {
            "src/energy_model.m": _script(rng, "energy_model", "live"),
            "old/energy_model.m": _script(rng, "energy_model", "old"),
            "archive/energy_model.m": _script(rng, "energy_model", "archived"),
        },
        "a live file with archived copies",
    )
    # lineage: a model carried through levels.
    family(
        "lineage",
        {
            f"models/{lvl}/drivetrain.mdl": mdl_text(
                "drivetrain",
                [{"type": "Gain", "name": f"g{lvl}", "params": {"Gain": str(rng.randint(1, 9))}}],
            )
            for lvl in ("L1", "L2", "L3")
        },
        "the plant model carried from level to level",
    )
    # succession: numbered versions of one report in one folder.
    family(
        "succession",
        {
            f"reports/energy_report_v{v}.md": (
                f"# Energy report v{v}\n\nEnergy {rng.randint(100, 999)} kJ.\n"
            )
            for v in (1, 2, 3)
        },
        "numbered versions; the newest is the default",
    )
    # compete: two different files side by side.
    family(
        "compete",
        {
            "analysis/fit_drag.m": _script(rng, "fit_drag", "a"),
            "analysis/fit_drag (1).m": _script(rng, "fit_drag", "b"),
        },
        "two different files in one folder; one must be current",
    )
    # compete trap: the same name at different depths (not a per-track fork).
    family(
        "compete",
        {
            "scripts/calibrate.m": _script(rng, "calibrate", "scripts"),
            "tools/misc/calibrate.m": _script(rng, "calibrate", "misc"),
        },
        "same name, different places that are not track forks",
    )
    # compete trap: per-track forks, but one track holds two different copies.
    first, second = tracks[0], tracks[1]
    family(
        "compete",
        {
            f"{first}/sweep.m": _script(rng, "sweep", "first"),
            f"{second}/sweep.m": _script(rng, "sweep", "second"),
            f"{second}/extra/sweep.m": _script(rng, "sweep", "deeper"),
        },
        "looks like a per-track fork, but one track holds two different copies",
    )
    # compete trap (1.1.0, review cp20-4-a F1): version-named folders are versions, not contexts.
    family(
        "compete",
        {
            "versions/v1/controller.m": _script(rng, "controller", "v1"),
            "versions/v2/controller.m": _script(rng, "controller", "v2"),
        },
        "successive versions in folders named v1 and v2; one must be current",
    )
    # compete trap (1.1.0, F3): one export beside a live file is no generated output.
    family(
        "compete",
        {
            "exports/slxdump/run3/layout.xml": f"<layout n='{rng.randint(1, 99)}'/>\n",
            "config/layout.xml": f"<layout n='{rng.randint(100, 199)}'/>\n",
        },
        "a generated export and a live file of the same name",
    )
    # Unrelated singletons (no family).
    files["README.md"] = "# Synthetic forking research repository\n"
    files["notes/plan.md"] = "Plan: compare the tracks.\n"

    root.mkdir(parents=True)
    for rel, text in sorted(files.items()):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return {
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "tracks": tracks,
        "families": families,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--golden", type=Path)
    args = parser.parse_args(argv)
    golden = generate(args.out, args.seed)
    target = args.golden or args.out.with_name(args.out.name + ".golden.json")
    target.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
