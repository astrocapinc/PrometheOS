"""Validate the portable design package, NOT the future PrometheOS implementation.

Python 3.12+, standard library only. Exit nonzero for inconsistent/missing artifacts.
The seal binds normative documents and fixtures, excluding mutable development state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
# Checkpoint and requirement counts per specification version; an amendment adds a row
# (1.1.0: amendment A01 added CP9, R18 and R19 — decision 0008;
#  1.2.0: amendment A02 added CP10-CP16 and R20-R34 — decision 0011;
#  1.3.0: amendment A03 clarifies CP10 authority/recovery — decision 0023;
#  1.4.0: amendment A04 states CP10 source admission and the interim POSIX refusal — decision 0026;
#  1.5.0: amendment A05 adds CP17-CP19 and R35-R37 and reorders CP13/CP15 — decision 0032;
#  1.5.1: amendment A05.1 registers the desktop shell (spec/09) and the restore-then-upgrade
#         contract (spec/08) — decision 0050;
#  1.5.2: amendment A05.2 moves the package README to spec/README.md and records the owner's
#         Apache-2.0 choice (spec/09) — decision 0072;
#  1.6.0: amendment A06.1 adds CP20 and R38, the research map (decisions 0080 and 0081);
#  1.7.0: amendment A07 adds CP21, CP22, R39 and R40, the web-first application and the
#         intelligence layer (decisions 0086 and 0087).
EXPECTED_IDS = {"1.0.0": (9, 17), "1.1.0": (10, 19), "1.2.0": (17, 34), "1.3.0": (17, 34), "1.4.0": (17, 34), "1.5.0": (20, 37), "1.5.1": (20, 37), "1.5.2": (20, 37), "1.6.0": (21, 38), "1.7.0": (23, 40)}
# A public export (tools/public_export.py, decision 0072) carries no development state or
# receipts (only the dependency register); it says so in this marker, and only then are the
# development-state checks skipped.
PUBLIC_EXPORT_MARKER = "PUBLIC-EXPORT.json"
# From CP10 on every checkpoint ends with an end-to-end walk check (A02, spec/10).
FIRST_WALK_CHECKPOINT = 10


def load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def indexed(rows: list, label: str) -> dict:
    result = {r["id"]: r for r in rows}
    require(len(result) == len(rows), f"Duplicate {label} IDs")
    return result


def inside_file(path: str) -> Path:
    candidate = (ROOT / path).resolve()
    require(candidate.is_relative_to(ROOT), f"Escaping package path: {path}")
    require(candidate.is_file(), f"Missing file: {path}")
    return candidate


def check_state(cps: dict, gates: dict) -> None:
    state = load("development/state.json")
    require(set(state["checkpoints"]) == set(cps), "State/checkpoint mismatch")
    require(set(state["external_gates"]) == set(gates), "State/external gate mismatch")
    for cp in state["checkpoints"].values():
        require(cp["technical"] in {"not_started", "in_progress", "failed", "passed"}, "Bad technical state")
        require(cp["review"] in {"pending", "changes_requested", "accepted"}, "Bad review state")
        if cp["receipt"] is not None:
            inside_file(cp["receipt"])
    for gate in state["external_gates"].values():
        require(gate["status"] in {"not_run", "blocked_external", "failed", "passed", "not_applicable"}, "Bad gate state")
    if not state["implementation_exists"]:
        require(all(c["technical"] == "not_started" and c["review"] == "pending" and c["receipt"] is None
                    for c in state["checkpoints"].values()), "Unimplemented package claims checkpoint evidence")
        require(all(g["status"] == "not_run" for g in state["external_gates"].values()), "Unimplemented package claims external evidence")
        require(state["public_release_authorized"] is False, "Package claims publication authority")


def main() -> None:
    manifest = load("package-manifest.json")
    require(manifest["spec_version"] in EXPECTED_IDS, "Unknown specification version")
    cp_count, req_count = EXPECTED_IDS[manifest["spec_version"]]
    require(bool(manifest["files"]), "Empty package seal")
    seen = set()
    for item in manifest["files"]:
        path = item["path"]
        require(path not in seen, f"Duplicate seal path: {path}")
        seen.add(path)
        actual = hashlib.sha256(inside_file(path).read_bytes()).hexdigest()
        require(actual == item["sha256"], f"Changed sealed file: {path}")

    graph = load("checkpoints.json")
    cps = indexed(graph["checkpoints"], "checkpoint")
    checks = indexed(graph["checks"], "check")
    gates = indexed(graph["external_gates"], "external gate")
    scenarios = indexed(load("acceptance/scenarios.json")["scenarios"], "scenario")
    reqs = indexed(load("requirements.json")["requirements"], "requirement")
    require(graph["spec_version"] == manifest["spec_version"], "checkpoints.json version differs from seal")
    require(set(cps) == {f"CP{i}" for i in range(cp_count)}, f"Expected CP0 through CP{cp_count - 1}")
    require(set(reqs) == {f"R{i:02}" for i in range(1, req_count + 1)}, f"Expected R01 through R{req_count:02}")
    delivery = (ROOT / "spec/10-delivery.md").read_text(encoding="utf-8")
    for cid, cp in cps.items():
        require(bool(cp["required_checks"]), f"No required checks: {cid}")
        require(cp["independent_review_required"] is True, f"Review omitted: {cid}")
        for dep in cp["depends_on"]:
            require(dep in cps and dep != cid, f"Invalid dependency: {cid}/{dep}")
        for check in cp["required_checks"]:
            require(check in checks and checks[check]["checkpoint"] == cid,
                    f"Bad check ownership: {cid}/{check}")
        for gate in cp["external_gates"]:
            require(gate in gates and gates[gate]["checkpoint"] == cid,
                    f"Bad external gate ownership: {cid}/{gate}")
        for task in cp["tasks"]:
            require(f"**{task}**" in delivery, f"Task card missing: {task}")
        if int(cid[2:]) >= FIRST_WALK_CHECKPOINT:
            require(f"C{cid[2:]}-WALK" in cp["required_checks"], f"Walk check missing: {cid}")
    for check, row in checks.items():
        require(check in cps[row["checkpoint"]]["required_checks"], f"Orphan check: {check}")
    visited, active = set(), set()

    def visit(cid: str) -> None:
        require(cid not in active, f"Checkpoint dependency cycle: {cid}")
        if cid in visited:
            return
        active.add(cid)
        for dep in cps[cid]["depends_on"]:
            visit(dep)
        active.remove(cid)
        visited.add(cid)

    for cid in cps:
        visit(cid)
    for sid, scenario in scenarios.items():
        require(all(c in checks for c in scenario["check_ids"]), f"Unknown check in {sid}")
        require(all(scenario.get(k) for k in ("given", "when", "then")), f"Incomplete {sid}")
        if scenario["fixture"]:
            inside_file(scenario["fixture"])
    for rid, req in reqs.items():
        inside_file(req["spec"])
        require(req["check_ids"] and all(c in checks for c in req["check_ids"]), f"Bad checks {rid}")
        require(req["scenario_ids"] and all(s in scenarios for s in req["scenario_ids"]), f"Bad scenarios {rid}")
        require(all(g in gates for g in req["external_gate_ids"]), f"Bad external gates {rid}")
    state_path = ROOT / "development" / "state.json"
    public_export = (ROOT / PUBLIC_EXPORT_MARKER).is_file() and not state_path.exists()
    if public_export:
        print("Public export: development state and receipts are not included; state checks skipped.")
    else:
        check_state(cps, gates)

    # Relative Markdown links must resolve without the old repository. Web links
    # are research references, not remotely executed or assumed available offline.
    # A public export keeps the decision records private: a link into development/ is
    # counted there, not failed.
    private_links = 0
    for relative in sorted(seen):
        if not relative.endswith(".md"):
            continue
        path = ROOT / relative
        content = path.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", content):
            target = target.strip("<>").split("#", 1)[0]
            if not target or target.startswith(("https://", "http://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if public_export and resolved.is_relative_to(ROOT / "development") and not resolved.exists():
                private_links += 1
                continue
            require(resolved.is_relative_to(ROOT) and resolved.exists(), f"Broken/nonportable link {path.name}: {target}")
    if private_links:
        print(f"Public export: {private_links} link(s) into the private development records not checked.")
    receipt = load("schemas/receipt.schema.json")
    require(receipt["$schema"].endswith("2020-12/schema"), "Unexpected receipt schema")
    require(receipt["additionalProperties"] is False, "Receipt must reject unknown fields")
    require({"candidate", "checks", "external_checks", "evidence", "review"} <= set(receipt["required"]), "Receipt fields missing")
    # Spec 1.1.0 declared CP9 while the receipt schema still only admitted CP0-CP8 (A02 fix).
    checkpoint_pattern = receipt["properties"]["checkpoint_id"]["pattern"]
    require(all(re.fullmatch(checkpoint_pattern, cid) for cid in cps),
            "Receipt schema rejects a declared checkpoint ID")
    require(len(load("design-inputs.json")["inputs"]) == 37, "Input provenance inventory mismatch")
    print(f"PASS: design package only; {len(seen)} sealed files, {len(cps)} checkpoints, "
          f"{len(checks)} checks, {len(reqs)} requirements, {len(scenarios)} scenarios.")
    print("No product implementation, live integration or researcher gate is certified by this check.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
