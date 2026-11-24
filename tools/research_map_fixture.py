"""A synthetic research repository that forks by track, for the CP20.5 walk (C20-WALK; decision
0107).

    uv run python tools/research_map_fixture.py OUT_DIR

It is shaped like the owner's kind of project, with every case C20-WALK names:

- **per-track forks**: ``track2/`` and ``track5/`` each hold the same simulation script and its
  output (``variant`` families), and a drive cycle copied into both (``identical``);
- **a stale run**: ``track5/run_sim.m`` changed after it wrote ``track5/energy.csv``;
- **a definitional pair**: ``results/uphill_energy.md`` gives two values for "uphill energy",
  on the stored cycle and on the published climb, which are two definitions, not a conflict;
- **a corrected number**: ``results/fuel_saving_v1.csv`` says the bus saves 18.2 % and
  ``results/fuel_saving_v2.csv`` corrects it to 17.4 %;
- **real competition** the owner must decide: ``analysis/fit_drag.m`` beside
  ``analysis/fit_drag (1).m``, and the named contexts ``vehicles/bus`` and ``vehicles/van``;
- notes that ask the research questions, a LaTeX paper, run logs and an archive copy.

Everything is deterministic: the same call writes the same bytes and modification times.
``GOLDEN`` says what the walk must find.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

BASE_TIME = 1_760_000_000  # 2025-10-09, fixed so the walk is reproducible

FILES: list[tuple[str, str, int]] = [
    # (path, text, seconds after BASE_TIME)
    ("README.md", "# Flywheel KERS study\n\n"
     "RQ1: Does a flywheel KERS cut the energy use of a city bus on the urban cycle?\n\n"
     "- Which ring damper keeps the flywheel stable at full speed?\n\n"
     "The work forks by track: track2 is the urban cycle, track5 the hilly route.\n", 0),
    ("paper/main.tex", "\\documentclass{article}\n"
     "\\title{Kinematic limits of a flywheel KERS}\n"
     "\\begin{document}\n\\maketitle\n\\input{results}\n\\end{document}\n", 100),
    ("paper/notes.md", "Q1: What limits the regenerative torque of the clutch?\n", 100),
    ("track2/run_sim.m", "% urban cycle\nT = table([196.9], 'VariableNames', {'kWh'});\n"
     "writetable(T, 'energy.csv');\n", 1000),
    ("track2/energy.csv", "kWh\n196.9\n", 1060),
    ("track2/drive_cycle.csv", "t,v\n0,0\n10,12.5\n20,0\n", 900),
    ("track5/run_sim.m", "% hilly route, damper 3e-4 (changed after the last run)\n"
     "T = table([241.3], 'VariableNames', {'kWh'});\nwritetable(T, 'energy.csv');\n", 5000),
    ("track5/energy.csv", "kWh\n238.0\n", 2000),  # older than its script: the stale run
    ("track5/drive_cycle.csv", "t,v\n0,0\n10,12.5\n20,0\n", 900),
    ("results/uphill_energy.md", "# Uphill energy\n\n"
     "uphill energy (stored cycle) = 196.9 MJ\n"
     "uphill energy (published climb) = 63.6 MJ\n", 3000),
    ("results/fuel_saving_v1.csv", "metric = value\nbus_fuel_saving_pct = 18.2\n", 3100),
    ("results/fuel_saving_v2.csv", "metric = value\nbus_fuel_saving_pct = 17.4\n", 3200),
    ("analysis/fit_drag.m", "% drag fit, least squares\ncd = 0.61;\n", 3300),
    ("analysis/fit_drag (1).m", "% drag fit, robust\ncd = 0.58;\n", 3350),
    ("vehicles/bus/fuel.csv", "vehicle,litres\nbus,31\n", 3400),
    ("vehicles/van/fuel.csv", "vehicle,litres\nvan,12\n", 3400),
    ("logs/run_20261001.log", "run 1 ok\n", 3500),
    ("logs/run_20261008.log", "run 2 ok\n", 3600),
    ("old/early_sim.m", "% an early copy\nT = table([190.0]);\n", 500),
]  # fmt: skip

GOLDEN: dict[str, Any] = {
    "tracks": ["track2", "track5"],
    "deliverable": "Kinematic limits of a flywheel KERS",
    "questions": [
        "Does a flywheel KERS cut the energy use of a city bus on the urban cycle?",
        "Which ring damper keeps the flywheel stable at full speed?",
        "What limits the regenerative torque of the clutch?",
    ],
    "stale_result": "track5/energy.csv",
    "stale_producer": "track5/run_sim.m",
    "compete_families": ["analysis/fit_drag", "vehicles fuel"],
    "definitional_pair": ("results/uphill_energy.md", 3, 4),
    "corrected": ("results/fuel_saving_v1.csv", "results/fuel_saving_v2.csv"),
}


def pointer(text: str, line: int, value: str) -> str:
    """``text:<line>:<start>:<end>`` of ``value`` on a 1-based line (0-based columns, end
    exclusive), as quantity bindings use."""
    content = text.splitlines()[line - 1]
    start = content.index(value)
    return f"text:{line}:{start}:{start + len(value)}"


def generate(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text, offset in FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        os.utime(path, (BASE_TIME + offset, BASE_TIME + offset))
    return {"root": str(root), "files": [f[0] for f in FILES], "golden": GOLDEN}


def text_of(rel: str) -> str:
    return next(t for p, t, _ in FILES if p == rel)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(generate(Path(sys.argv[1]))["root"])
