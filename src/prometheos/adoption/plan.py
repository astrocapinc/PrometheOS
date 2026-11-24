"""The consolidation plan (A05, CP17.5; decision 0043; C17-PLAN, S50).

A plan is typed work derived deterministically from one scan generation: what the owner must
decide and what the coding agent can do with its own tools. Every step declares its inputs
(paths with content digests), the actions it allows and forbids, the schema of its result and its
acceptance criteria, and it is created as a CP18 work item (a proposed ``action``) that the owner
sees in Work items and the agent sees in the queue. Nothing here writes into the adopted folder,
deletes, moves or deduplicates anything: those stay proposals for the owner.

Results: the agent reports a step's result; PrometheOS validates it against the step's closed
schema and the generation's inventory (paths and digests) and records it ``validated`` or
``rejected``. The owner decides families (T0) and accepts validated results with a baseline;
that makes the next plan version, which no longer asks for what was settled.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from prometheos.adoption import classes, tables
from prometheos.adoption.contracts import (
    MEMBER_SEPARATOR,
    AdoptionFamily,
    AdoptionFile,
    RelPath,
    check_relative,
)
from prometheos.domain.canonical import canonical_sha256, dumps_canonical
from prometheos.domain.models import PrometheosError, Sha256, StrictModel, utc_now

StepKind = Literal[
    "resolve_conflict",
    "tool_extract",
    "rerun",
    "recompute_verify",
    "compare",
    "metadata_proposal",
    "backup_gap",
    "hydrate_or_skip",
    "dedupe_proposal",
    "convention_proposal",
]
STEP_KINDS: tuple[str, ...] = StepKind.__args__  # type: ignore[attr-defined]
StepState = Literal[
    "ready", "reported", "validated", "rejected", "accepted", "settled", "superseded"
]
Actor = Literal["owner", "agent"]
MAX_STEPS = 60
ALWAYS_FORBIDDEN = (
    "write_into_adopted_folder",
    "move_or_rename_in_adopted_folder",
    "delete",
    "hydrate_cloud_files",
    "network_without_owner_approval",
)
SCHEMA_VERSION = "plan-steps-1"
TakenAction = Literal[
    "adoption_read",
    "adoption_manage.assist_submit",
    "matlab_read_only",
    "copy_inputs_to_scratch",
    "run_producer_in_scratch",
    "matlab",
    "write_into_adopted_folder",
    "move_or_rename_in_adopted_folder",
    "delete",
    "hydrate_cloud_files",
    "network_without_owner_approval",
]
"""What an agent says it did for a step: the allowed actions of the plan's vocabulary and the
forbidden ones, so a report that admits a forbidden action is rejected, not parsed from prose."""
ActionsTaken = Annotated[list[TakenAction], Field(min_length=1, max_length=20)]


class StepInput(StrictModel):
    path: RelPath
    sha256: Sha256 | None


class PlanStep(StrictModel):
    """One step (table ``adoption_steps``, column ``payload``)."""

    step_id: Annotated[str, StringConstraints(pattern=r"^st_[0-9a-f]{16}$")]
    kind: StepKind
    actor: Actor
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    why: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    family_ids: list[Annotated[str, StringConstraints(pattern=r"^fam_[0-9a-f]{16}$")]] = Field(
        max_length=200
    )
    inputs: list[StepInput] = Field(max_length=500)
    procedure: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    allowed: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        max_length=20
    )
    forbidden: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=20
    )
    output_schema: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    acceptance: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=10
    )
    report_via: Annotated[str, StringConstraints(min_length=1, max_length=80)]

    @model_validator(mode="after")
    def _forbids(self) -> PlanStep:
        missing = [f for f in ALWAYS_FORBIDDEN if f not in self.forbidden]
        if missing:
            raise ValueError(f"every step forbids {missing}")
        if self.actor == "owner" and self.report_via not in ("adoption_decide", "owner"):
            raise ValueError("an owner step is settled by the owner's decision")
        return self


# ----- result schemas (closed) ---------------------------------------------------------------

Short = Annotated[str, StringConstraints(min_length=1, max_length=400)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class RerunOutput(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    sha256: Sha256


class RerunResult(StrictModel):
    """A producer re-run outside the adopted folder: what ran and the outputs' digests."""

    command: Short
    exit_code: Annotated[int, Field(ge=-(2**31), le=2**31)]
    tool_version: Short | None
    outputs: list[RerunOutput] = Field(max_length=200)
    actions_taken: ActionsTaken


class CompareResult(StrictModel):
    compared: list[RelPath] = Field(min_length=1, max_length=50)
    metric: Short
    max_difference: float
    tolerance: float
    within_tolerance: bool
    actions_taken: ActionsTaken


class ProposalResult(StrictModel):
    """A proposal for the owner (metadata, conventions, dedupe, backup restore): text and the
    inventory paths it concerns. Never applied by PrometheOS."""

    summary: Short
    proposal: Text
    paths: list[RelPath] = Field(max_length=1000)
    actions_taken: ActionsTaken


class ToolExtractResult(StrictModel):
    """Which reading tasks were submitted (their results are validated by the assist path)."""

    assist_ids: list[Annotated[str, StringConstraints(pattern=r"^as_[0-9a-f]{32}$")]] = Field(
        min_length=1, max_length=200
    )
    actions_taken: ActionsTaken


RESULT_SCHEMAS: dict[str, type[StrictModel]] = {
    "rerun_result": RerunResult,
    "compare_result": CompareResult,
    "proposal_result": ProposalResult,
    "tool_extract_result": ToolExtractResult,
}


# ----- generation ----------------------------------------------------------------------------


def plan_id_for(root_alias: str) -> str:
    return "plan_" + hashlib.sha256(root_alias.encode()).hexdigest()[:16]


def step_id_for(plan_id: str, kind: str, key: str) -> str:
    return "st_" + hashlib.sha256(f"{plan_id}\0{kind}\0{key}".encode()).hexdigest()[:16]


def _inputs(rows: dict[str, AdoptionFile], paths: list[str]) -> list[dict[str, Any]]:
    return [{"path": p, "sha256": rows[p].sha256 if p in rows else None} for p in sorted(paths)][
        :500
    ]


def _step(plan_id: str, kind: str, key: str, **values: Any) -> PlanStep:
    forbidden = list(dict.fromkeys([*ALWAYS_FORBIDDEN, *values.pop("forbidden", [])]))
    return PlanStep.model_validate(
        {"step_id": step_id_for(plan_id, kind, key), "kind": kind, "forbidden": forbidden, **values}
    )


def derive(
    root_alias: str,
    rows: list[AdoptionFile],
    families: list[AdoptionFamily],
    evidence: list[Any],
    assist_tasks: list[dict[str, Any]],
    settled: dict[str, str],
    reruns: dict[str, dict[str, Any]] | None = None,
    class_decided: frozenset[str] = frozenset(),
) -> tuple[list[PlanStep], int]:
    """The steps one generation needs, most important first, and how many owner decisions did
    not fit. ``settled``: steps the owner settled (decided or accepted) with the digest of the
    inputs they were settled on; they are asked again only when their inputs changed, and a
    family's decision step only when the decision no longer applies (review cp17-5-a F2, F7).
    ``reruns``: re-run outcomes by family; one that did not reproduce the current result adds
    the compare and recompute-and-verify steps (F4)."""
    plan_id = plan_id_for(root_alias)
    by_path = {r.path: r for r in rows}
    steps: list[PlanStep] = []

    # owner decisions: undecided families and kept contradictions
    digests = {r.path: {"sha256": r.sha256} for r in rows}  # built once (F8)
    for family in families:
        if family.deciding_rule == "R1":
            continue  # the owner decided it; kept contradictions stay visible in the family
        if family.family_id in class_decided:
            continue  # the owner said variant, noise or split
        # A06.1 (CP20.4, decision 0085): only real competition and an undecided succession ask
        # the owner; every other class needs no decision (C17-PLAN as amended). A settled ranking
        # still asks when its current is not the class's default (review r2 R2-F2).
        found = classes.classify(family.model_dump(mode="json"), digests)
        quiet = family.band in ("decided", "supported") and not family.conflicts
        if (quiet and not found["off_default"]) or not found["files_item"]:
            continue
        reason = (
            "no member could be singled out"
            if family.current is None
            else f"{len(family.conflicts)} contradiction(s) kept; band {family.band}"
        )
        steps.append(
            _step(
                plan_id,
                "resolve_conflict",
                family.family_id,
                actor="owner",
                title=f"Decide the current file of {family.key}",
                why=reason,
                family_ids=[family.family_id],
                inputs=_inputs(by_path, [m.path for m in family.members]),
                procedure="Read the family in the hub (Adoption) with its reasons and kept "
                "contradictions, then choose the current member (adoption_decide). The agent may "
                "summarise the evidence when asked; it never decides.",
                allowed=["adoption_read"],
                output_schema="owner_decision",
                acceptance=["the owner chose a member (T0)"],
                report_via="adoption_decide",
            )
        )

    # reading tasks the agent can do with its own tools
    open_tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in assist_tasks:
        if not task["done"]:
            open_tasks[task["task_kind"]].append(task)
    for kind, tasks in sorted(open_tasks.items()):
        subjects = [t["subject"] for t in tasks]
        steps.append(
            _step(
                plan_id,
                "tool_extract",
                kind,
                actor="agent",
                title=f"Reading tasks: {kind.replace('_', ' ')} ({len(tasks)})",
                why=tasks[0]["reason"],
                family_ids=[],
                inputs=_inputs(by_path, subjects),
                procedure="For each input: adoption_read assist_prepare, read the numbered lines "
                "or run the tool as the task says, and send each sample with adoption_manage "
                "assist_submit. Copy quotes exactly; give independent samples.",
                allowed=["adoption_read", "adoption_manage.assist_submit", "matlab_read_only"],
                output_schema="tool_extract_result",
                acceptance=["every submitted task is accepted or sent to the owner by PrometheOS"],
                report_via="adoption_step_report",
            )
        )

    # re-runs: a result family whose current member a producer wrote
    producers: dict[str, set[str]] = defaultdict(set)
    for e in evidence:
        if e.predicate == "generated_by" and e.object.path:
            producers[e.subject].add(e.object.path)
    for family in families:
        if family.current is None or family.kind != "data":
            continue
        made_by = sorted(producers.get(family.current, set()))
        if not made_by:
            continue
        steps.append(
            _step(
                plan_id,
                "rerun",
                family.family_id,
                actor="agent",
                title=f"Re-run {made_by[0]} to confirm {family.current}",
                why=f"the current result is proposed by rule {family.deciding_rule}; a re-run "
                "outside the folder checks it",
                family_ids=[family.family_id],
                inputs=_inputs(by_path, [*made_by, family.current]),
                procedure="Copy the inputs to a scratch folder outside the adopted folder, run the "
                "producer there with the researcher's tools, and report the command, exit code, "
                "tool version and the SHA-256 of each output (adoption_step_report).",
                allowed=[
                    "adoption_read",
                    "copy_inputs_to_scratch",
                    "run_producer_in_scratch",
                    "matlab",
                ],
                output_schema="rerun_result",
                acceptance=[
                    "exit code 0",
                    "an output named like the current result",
                    "its digest equals the current result, or a compare step follows",
                ],
                report_via="adoption_step_report",
            )
        )

    # re-runs of stale results (A06.1, CP20.4 6c; decision 0106): a result whose producer
    # changed after it was written, whatever its family, is the outline's stale candidate; a
    # re-run outside the folder settles it
    covered = {i.path for st in steps if st.kind == "rerun" for i in st.inputs}
    for subject, made in sorted(producers.items()):
        result = by_path.get(subject)
        if result is None or result.status != "read" or subject in covered:
            continue
        written = result.mtime_us
        newer = sorted(
            p for p in made
            if written is not None and p in by_path
            and (by_path[p].mtime_us or 0) > written
        )  # fmt: skip
        if not newer:
            continue
        steps.append(
            _step(
                plan_id,
                "rerun",
                f"stale:{subject}",
                actor="agent",
                title=f"Re-run {newer[0]}: {subject} is older than it",
                why=f"{newer[0]} changed after it wrote {subject}, so the result may no longer "
                "be what the script gives",
                family_ids=[],
                inputs=_inputs(by_path, [*newer, subject]),
                procedure="Copy the inputs to a scratch folder outside the adopted folder, run the "
                "producer there with the researcher's tools, and report the command, exit code, "
                "tool version and the SHA-256 of each output (adoption_step_report).",
                allowed=[
                    "adoption_read",
                    "copy_inputs_to_scratch",
                    "run_producer_in_scratch",
                    "matlab",
                ],
                output_schema="rerun_result",
                acceptance=[
                    "exit code 0",
                    f"an output named like {subject}",
                    "its digest equals the adopted result, or a compare step follows",
                ],
                report_via="adoption_step_report",
            )
        )

    # dedupe proposal: exact copies (one step)
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.entry == "file" and row.status == "read" and row.sha256 and row.size:
            groups[row.sha256].append(row.path)
    dupes = {sha: sorted(paths) for sha, paths in groups.items() if len(paths) > 1}
    if dupes:
        extra = sum((len(p) - 1) * (by_path[p[0]].size or 0) for p in dupes.values())
        steps.append(
            _step(
                plan_id,
                "dedupe_proposal",
                "all",
                actor="agent",
                title=f"Propose how to deduplicate {len(dupes)} groups of identical files",
                why=f"{sum(len(p) for p in dupes.values())} files in {len(dupes)} groups; "
                f"{extra} bytes are extra copies",
                family_ids=[],
                inputs=_inputs(by_path, [p for g in dupes.values() for p in g]),
                procedure="Write a proposal: which copy to keep in each group and why, and what "
                "the researcher would delete or move. Never delete or move anything.",
                allowed=["adoption_read"],
                output_schema="proposal_result",
                acceptance=[
                    "every path is an inventory path of an identical group",
                    "nothing was deleted or moved",
                ],
                report_via="adoption_step_report",
            )
        )

    # backup gaps: families whose members all sit in backup folders
    for family in families:
        zones = {by_path[m.path].zone for m in family.members if m.path in by_path}
        if zones == {"backup_copy"}:
            steps.append(
                _step(
                    plan_id,
                    "backup_gap",
                    family.family_id,
                    actor="agent",
                    title=f"{family.key} exists only in backup folders",
                    why="no member outside a backup folder",
                    family_ids=[family.family_id],
                    inputs=_inputs(by_path, [m.path for m in family.members]),
                    procedure="Compare the backup copies and propose whether one should be "
                    "restored "
                    "into the working folder; the researcher restores it, never the agent.",
                    allowed=["adoption_read"],
                    output_schema="proposal_result",
                    acceptance=["a restore proposal naming every backup copy (inventory paths)"],
                    report_via="adoption_step_report",
                )
            )

    # cloud placeholders: hydrate or skip (one owner step)
    placeholders = [r.path for r in rows if r.status == "placeholder"]
    if placeholders:
        steps.append(
            _step(
                plan_id,
                "hydrate_or_skip",
                "all",
                actor="owner",
                title=f"Decide whether to download {len(placeholders)} cloud placeholder(s)",
                why="placeholders are recorded but never opened; their content is unknown",
                family_ids=[],
                inputs=_inputs(by_path, placeholders),
                procedure="Download the files you need with your sync client (PrometheOS never "
                "does), then scan again; or leave them as placeholders.",
                allowed=[],
                output_schema="owner_decision",
                acceptance=["the owner chose to download and rescan, or to skip"],
                report_via="owner",
            )
        )

    # metadata proposal: decided data families without a producer record
    bare = [f for f in families if f.kind == "data" and f.current and not producers.get(f.current)]
    if bare:
        steps.append(
            _step(
                plan_id,
                "metadata_proposal",
                "data",
                actor="agent",
                title=f"Propose run records for {len(bare)} result "
                f"{'family' if len(bare) == 1 else 'families'} without a producer",
                why="no code in the folder names these results; a run-manifest sidecar would",
                family_ids=[f.family_id for f in bare][:200],
                inputs=_inputs(by_path, [f.current for f in bare if f.current]),
                procedure="Draft a run-manifest (producer, inputs with digests, parameters, tool "
                "versions) for each, as text in the result; nothing is written into the folder.",
                allowed=["adoption_read"],
                output_schema="proposal_result",
                acceptance=[
                    "a draft for every result without a producer, naming its inventory path"
                ],
                report_via="adoption_step_report",
            )
        )

    # conventions: names that mislead (FINAL, BEST, copies, dated backups)
    misleading = sorted(
        r.path
        for r in rows
        if r.entry == "file"
        and any(t in r.path.lower() for t in ("final", "best", " - copy", "_old", "backup"))
        and MEMBER_SEPARATOR not in r.path
    )
    if misleading:
        steps.append(
            _step(
                plan_id,
                "convention_proposal",
                "names",
                actor="agent",
                title="Propose conventions for naming and status going forward",
                why=f"{len(misleading)} file names carry FINAL/BEST/copy/backup marks that rank "
                "nothing",
                family_ids=[],
                inputs=_inputs(by_path, misleading[:200]),
                procedure="Propose a short convention (status in a note or frontmatter, a run "
                "manifest per result, no FINAL/BEST names); the researcher adopts it or not.",
                allowed=["adoption_read"],
                output_schema="proposal_result",
                acceptance=["a proposal the researcher can read in one screen"],
                report_via="adoption_step_report",
            )
        )

    # re-runs that did not reproduce: compare the outputs, then recompute and verify (F4)
    used_by: dict[str, set[str]] = defaultdict(set)
    for e in evidence:
        if e.predicate in ("used", "references") and e.object.path and e.determinism == "pure":
            used_by[e.object.path].add(e.subject)
    for family in families:
        outcome = (reruns or {}).get(family.family_id)
        if family.current is None or not outcome or outcome.get("reproduced") is not False:
            continue
        steps.append(
            _step(
                plan_id,
                "compare",
                family.family_id,
                actor="agent",
                title=f"Compare the re-run output with {family.current}",
                why="the re-run did not reproduce the current result byte for byte",
                family_ids=[family.family_id],
                inputs=_inputs(by_path, [family.current]),
                procedure="Load the re-run output (in the scratch folder) and the current result "
                "read-only, compare them with a stated metric and tolerance, and report it.",
                allowed=["adoption_read", "matlab", "matlab_read_only"],
                output_schema="compare_result",
                acceptance=["a metric, its largest difference and the tolerance are stated"],
                report_via="adoption_step_report",
            )
        )
        consumers = sorted(used_by.get(family.current, set()))[:20]
        steps.append(
            _step(
                plan_id,
                "recompute_verify",
                family.family_id,
                actor="agent",
                title=f"Recompute what uses {family.current} and verify it",
                why="the result changed when re-run; what is derived from it may change too",
                family_ids=[family.family_id],
                inputs=_inputs(by_path, [family.current, *consumers]),
                procedure="In the scratch folder, recompute the figures or tables derived from "
                "the result with the re-run output, compare them with the stored ones and "
                "report the comparison; nothing in the adopted folder changes.",
                allowed=[
                    "adoption_read",
                    "copy_inputs_to_scratch",
                    "run_producer_in_scratch",
                    "matlab",
                ],
                output_schema="compare_result",
                acceptance=["every recomputed output is compared within a stated tolerance"],
                report_via="adoption_step_report",
            )
        )

    fresh = [
        s
        for s in steps
        if s.kind == "resolve_conflict" or settled.get(s.step_id) != inputs_digest(s)
    ]
    # Owner decisions fill what is left after every other kind (F6): nothing else is crowded
    # out, and the omitted count is reported, never silent.
    others = [s for s in fresh if s.kind != "resolve_conflict"]
    decisions = [s for s in fresh if s.kind == "resolve_conflict"]
    room = max(0, MAX_STEPS - len(others))
    return decisions[:room] + others, max(0, len(decisions) - room)


# ----- validation of reported results --------------------------------------------------------


def validate_result(
    step: PlanStep,
    result: Any,
    rows: dict[str, AdoptionFile],
    assist_subjects: dict[str, tuple[str, str | None]] | None = None,
) -> tuple[bool, list[str], dict[str, Any] | None]:
    """A reported result against the step's closed schema and the inventory."""
    if step.actor != "agent":
        return False, ["this step is the owner's decision"], None
    model_type = RESULT_SCHEMAS.get(step.output_schema)
    if model_type is None:  # pragma: no cover - derive never builds such a step
        return False, ["no result schema"], None
    try:
        model = model_type.model_validate(result)
    except ValidationError as exc:
        return False, [f"schema: {exc.error_count()} error(s)"], None
    problems: list[str] = []
    inputs = {i.path for i in step.inputs}
    digests = {i.path: i.sha256 for i in step.inputs}
    if isinstance(model, ProposalResult | CompareResult):
        named = model.paths if isinstance(model, ProposalResult) else model.compared
        for path in named:
            check_relative(path)
            if path not in rows:
                problems.append(f"not an inventory path: {path[:120]}")
            elif path not in inputs:
                problems.append(f"not one of the step's inputs: {path[:120]}")
    if isinstance(model, ProposalResult) and inputs and not model.paths:
        problems.append("a proposal names at least one of the step's inputs")  # F3
    if isinstance(model, ProposalResult) and step.kind in (
        "dedupe_proposal",
        "metadata_proposal",
        "backup_gap",
    ):
        missing = inputs - set(model.paths)
        if missing:  # a dedupe proposal covers every copy (review cp17-5-a-r2 R2-2)
            problems.append(f"the proposal leaves out {len(missing)} of the step's files")
    if isinstance(model, ToolExtractResult):
        # Every assist ID must be a task PrometheOS recorded for one of the step's inputs (F3).
        for assist_id in model.assist_ids:
            found = (assist_subjects or {}).get(assist_id)
            if found is None:
                problems.append(f"no accepted reading task: {assist_id}")
            elif found[0] not in inputs:
                problems.append(f"the reading task is about another file: {assist_id}")
            elif found[1] != digests.get(found[0]):
                # read for other content than the step's input now (review cp17-5-a-r3 R3-3)
                problems.append(f"the reading task was about an earlier version: {assist_id}")
    if isinstance(model, RerunResult):
        if model.exit_code != 0:
            problems.append(f"the producer exited with {model.exit_code}")
        current = next((i for i in step.inputs if i.path not in _producers(step)), None)
        names = {o.name for o in model.outputs}
        if current is not None and current.path.rsplit("/", 1)[-1] not in names:
            problems.append("no output named like the current result")
    taken: list[str] = list(getattr(model, "actions_taken", []))
    for action in sorted(set(taken)):
        if action in ALWAYS_FORBIDDEN:
            problems.append(f"the report says it did {action!r}, which every adoption step forbids")
        elif action not in step.allowed:
            problems.append(f"{action!r} is not an allowed action of this step")
    return not problems, problems, model.model_dump(mode="json") if not problems else None


def _producers(step: PlanStep) -> set[str]:
    return {i.path for i in step.inputs if i.path.lower().endswith((".m", ".py", ".r", ".jl"))}


def rerun_outcome(step: PlanStep, output: dict[str, Any]) -> dict[str, Any]:
    """Whether a validated re-run reproduced the current result exactly."""
    current = next((i for i in step.inputs if i.path not in _producers(step)), None)
    if current is None:
        return {"reproduced": None}
    name = current.path.rsplit("/", 1)[-1]
    digests = {o["sha256"] for o in output["outputs"] if o["name"] == name}
    return {"reproduced": current.sha256 in digests, "current": current.path}


# ----- storage -------------------------------------------------------------------------------


def latest_version(conn: sqlite3.Connection, plan_id: str) -> int:
    value = conn.execute(
        "SELECT MAX(version) FROM adoption_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()[0]
    return int(value or 0)


def step_rows(conn: sqlite3.Connection, plan_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM adoption_steps WHERE plan_id = ? ORDER BY step_id", (plan_id,)
    ).fetchall()
    return {
        r["step_id"]: {
            "step": PlanStep.model_validate(json.loads(r["payload"])),
            "state": r["state"],
            "action_id": r["action_id"],
            "plan_version": r["plan_version"],
            "result": json.loads(r["result"]) if r["result"] else None,
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


def inputs_digest(step: PlanStep) -> str:
    return canonical_sha256([[i.path, i.sha256] for i in step.inputs])


def settled_ids(conn: sqlite3.Connection, plan_id: str) -> dict[str, str]:
    """Settled or accepted steps with the digest of the inputs they were settled on."""
    return {
        sid: inputs_digest(row["step"])
        for sid, row in step_rows(conn, plan_id).items()
        if row["state"] in ("accepted", "settled")
    }


def rerun_outcomes(conn: sqlite3.Connection, plan_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in step_rows(conn, plan_id).values():
        step = row["step"]
        if step.kind == "rerun" and row["state"] in ("validated", "accepted") and row["result"]:
            for fid in step.family_ids:
                out[fid] = row["result"].get("outcome") or {}
    return out


def save_version(
    conn: sqlite3.Connection,
    plan_id: str,
    scan_gen: int,
    steps: list[PlanStep],
    created_by: str,
    omitted: int = 0,
) -> int:
    version = latest_version(conn, plan_id) + 1
    known = step_rows(conn, plan_id)
    now = utc_now()
    current = {s.step_id for s in steps}
    for sid, row in known.items():
        if sid not in current and row["state"] not in ("accepted", "settled", "superseded"):
            conn.execute(
                "UPDATE adoption_steps SET state = 'superseded', updated_at = ? WHERE step_id = ?",
                (now, sid),
            )
    for step in steps:
        payload = dumps_canonical(step.model_dump(mode="json")).decode()
        if step.step_id in known:
            old = known[step.step_id]
            state, result = old["state"], old["result"]
            changed = inputs_digest(old["step"]) != inputs_digest(step)
            if (
                state == "superseded"
                or (changed and state != "ready")
                or (step.kind == "resolve_conflict" and state == "settled")
            ):
                # Reopened: its inputs changed since the result or decision, or the owner's
                # decision no longer applies (review cp17-5-a F2, F7). The old result is kept
                # as history, never accepted for the new inputs.
                state = "ready"
                result = {"previous": result} if result else None
            conn.execute(
                "UPDATE adoption_steps SET plan_version = ?, payload = ?, updated_at = ?, "
                "state = ?, result = ? WHERE step_id = ?",
                (version, payload, now, state,
                 dumps_canonical(result).decode() if result is not None else None, step.step_id),
            )  # fmt: skip
        else:
            conn.execute(
                "INSERT INTO adoption_steps (step_id, plan_id, plan_version, kind, action_id, "
                "state, payload, result, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, 'ready', ?, NULL, ?)",
                (step.step_id, plan_id, version, step.kind, payload, now),
            )
    summary = {
        "scan_gen": scan_gen,
        "created_by": created_by[:100],
        "schema": SCHEMA_VERSION,
        "steps": [s.step_id for s in steps],
        "by_kind": dict(sorted(_count(s.kind for s in steps).items())),
        "omitted_decisions": omitted,
    }
    conn.execute(
        "INSERT INTO adoption_plans (plan_id, version, scan_gen, state, payload, created_at) "
        "VALUES (?, ?, ?, 'open', ?, ?)",
        (plan_id, version, scan_gen, dumps_canonical(summary).decode(), now),
    )
    return version


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


# A06.1 (CP20.4 6c; decision 0106): agent steps that are checks become verification actions,
# with their kind and estimated minutes (a rerun takes its own time, so it has no estimate and
# always needs the owner). The other agent steps are proposals and stay ordinary work items.
# The value is the kind and the read-only estimate per input file (at least 5 minutes in all).
STEP_CHECKS: dict[str, tuple[str, int | None]] = {
    "compare": ("read_only", 3),
    "tool_extract": ("read_only", 2),
    "recompute_verify": ("rerun", None),
    "rerun": ("rerun", None),
}


def check_minutes(step: PlanStep) -> int | None:
    per_input = STEP_CHECKS[step.kind][1]
    return None if per_input is None else max(5, min(600, per_input * len(step.inputs)))


def is_check(step: PlanStep) -> bool:
    return step.actor != "owner" and step.kind in STEP_CHECKS


def work_item_body(step: PlanStep) -> dict[str, Any]:
    """The CP18 work item of a step: a proposed action the owner adopts or not; a check the
    agent can do is a verification action (CP20.3's budget applies)."""
    lines = [
        step.why,
        "",
        "Who: "
        + ("the researcher decides" if step.actor == "owner" else "the coding agent, when asked"),
        f"How: {step.procedure}",
        "Inputs: "
        + ", ".join(i.path for i in step.inputs[:20])
        + (f" (+{len(step.inputs) - 20} more)" if len(step.inputs) > 20 else ""),
        "Never: " + ", ".join(f.replace("_", " ") for f in step.forbidden),
        "Done when: " + "; ".join(step.acceptance),
        f"Plan step {step.step_id} ({step.kind}); report with {step.report_via}.",
    ]
    return {
        "title": f"Adoption: {step.title}"[:200],
        "reason": f"Consolidation plan step ({step.kind.replace('_', ' ')})",
        "source_refs": [],
        "owner_ref": None,
        "due_at": None,
        "dependencies": [],
        "status": "proposed",
        "blocker": None,
        "completion_evidence": [],
        "commitment_item_id": None,
        "description": "\n".join(lines)[:20_000],
        "labels": ["adoption", step.kind.replace("_", "-")]
        + (["verification"] if is_check(step) else []),
    } | (
        {
            "verification": {
                "adoption_step": step.step_id,
                "kind": STEP_CHECKS[step.kind][0],
                "estimated_minutes": check_minutes(step),
                "expected": "; ".join(step.acceptance)[:2000],
            }
        }
        if is_check(step)
        else {}
    )


def baseline_payload(
    conn: sqlite3.Connection, scan_gen: int, plan_id: str, version: int
) -> dict[str, Any]:
    """What an accepted baseline fixes: every family with its current member and digests, the
    decisions, the validated results it accepts, and the inventory counts."""
    scan = tables.require_scan(conn, scan_gen)
    rows = {r.path: r for r in tables.all_files(conn, scan_gen)}
    families = tables.list_families(conn, scan_gen)
    decided = class_decisions(conn, scan_gen)
    digests = digest_map(rows)
    return {
        "root_alias": scan.root_alias,
        "scan_gen": scan_gen,
        "root_digest": scan.root_digest_after,
        "plan": {"plan_id": plan_id, "version": version},
        "families": [
            {
                "family_id": f.family_id,
                "key": f.key,
                "band": f.band,
                "current": f.current,
                "current_sha256": rows[f.current].sha256 if f.current in rows else None,
                "members": sorted(
                    (m.path, rows[m.path].sha256 if m.path in rows else None) for m in f.members
                ),
                # A06.1 (CP20.4): the class by the rules and the owner's class decision, if any.
                "class": family_class(f, digests, decided)["class"],
                "class_decision": decided.get(f.family_id),
            }
            for f in families
        ],
        "files": len([r for r in rows.values() if r.entry == "file"]),
        # content only: the scan's duration is left out, so equal folders give equal digests
        "summary_sha256": canonical_sha256(
            scan.summary.model_dump(mode="json", exclude={"seconds"})
        )
        if scan.summary
        else None,
    }


# ----- family classes and the owner's class decisions (A06.1, CP20.4; decision 0085) ---------

CLASS_DECISIONS = ("variant", "noise", "split", "revoked")


def _member_set(family: AdoptionFamily, rows: dict[str, AdoptionFile]) -> str:
    """The family's exact members with their digests, canonical: a class decision applies only
    to this set."""
    return dumps_canonical(
        sorted([m.path, rows[m.path].sha256 if m.path in rows else None] for m in family.members)
    ).decode()


def class_decisions(conn: sqlite3.Connection, scan_gen: int) -> dict[str, str]:
    """Family ID -> the owner's latest class decision that still applies in this generation
    (same key, same members and digests)."""
    scan = tables.require_scan(conn, scan_gen)
    rows = {r.path: r for r in tables.all_files(conn, scan_gen)}
    latest: dict[tuple[str, str], str] = {}
    for key, members, decision in conn.execute(
        "SELECT family_key, members, decision FROM adoption_class_decisions WHERE root_alias = ? "
        "ORDER BY decided_at, decision_id",
        (scan.root_alias,),
    ):
        latest[(str(key), str(members))] = str(decision)
    out: dict[str, str] = {}
    for family in tables.list_families(conn, scan_gen):
        found = latest.get((family.key, _member_set(family, rows)))
        if found is not None and found != "revoked":
            out[family.family_id] = found
    return out


def family_class(
    family: AdoptionFamily, digests: dict[str, dict[str, Any]], decided: dict[str, str]
) -> dict[str, Any]:
    """The family's class by the rules, with the owner's class decision when one applies (it
    then files no item). ``digests``: path -> {"sha256"}, built once per read (F8)."""
    found = classes.classify(family.model_dump(mode="json"), digests)
    decision = decided.get(family.family_id)
    if decision is not None:
        found = found | {"decision": decision, "files_item": False}
    return found | {"decision": decision}


def digest_map(rows: dict[str, AdoptionFile]) -> dict[str, dict[str, Any]]:
    return {p: {"sha256": r.sha256} for p, r in rows.items()}


def decide_class(
    store: Any,
    scan_gen: int,
    family_id: str,
    decision: str,
    note: str | None,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """The owner's class decision for a family: variant (each member is current in its own
    context), noise (none of them matters) or split (they are not one family). It files no item,
    settles the family's decision step and is recorded in the next baseline."""
    from prometheos.domain.ids import new_request_id
    from prometheos.store import capabilities, db
    from prometheos.store.project import ProjectStore

    if decision not in CLASS_DECISIONS:
        raise PrometheosError(
            "validation_error",
            "decide variant, noise or split (or revoked to undo)",
            {"decision": decision[:20]},
        )
    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise PrometheosError("validation_error", "a note is 1..2000 characters", {})
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            scan = tables.require_scan(conn, scan_gen)
            family = tables.get_family(conn, scan_gen, family_id)
            rows = {r.path: r for r in tables.all_files(conn, scan_gen)}
            if any(rows.get(m.path) is None or rows[m.path].sha256 is None for m in family.members):
                raise PrometheosError(
                    "validation_error",
                    "a member has no digest, so a decision could not notice it change",
                    {"family_id": family_id},
                )
            applied = class_decisions(conn, scan_gen)
            if decision == "revoked" and family_id not in applied:
                raise PrometheosError(
                    "validation_error", "no class decision of this family is in force", {}
                )
            if grant is not None:
                capabilities.consume(
                    conn,
                    secret=grant,
                    project_id=ProjectStore.project_id_in(conn),
                    operation="exact_request",
                    digest=grant_digest,
                )
            conn.execute(
                "INSERT INTO adoption_class_decisions (decision_id, root_alias, family_key, "
                "members, decision, note, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "cdec_" + new_request_id().replace("-", "")[:24],
                    scan.root_alias,
                    family.key,
                    _member_set(family, rows),
                    decision,
                    note.strip() if note else None,
                    actor[:100],
                    utc_now(),
                ),
            )
            step_id = step_id_for(plan_id_for(scan.root_alias), "resolve_conflict", family_id)
            if decision == "revoked":
                # Review r2 R2-F3: an undo reopens only the step a class decision settled.
                conn.execute(
                    "UPDATE adoption_steps SET state = 'ready', updated_at = ?, result = NULL "
                    "WHERE step_id = ? AND state = 'settled' AND result LIKE '%class_decision%'",
                    (utc_now(), step_id),
                )
            else:
                conn.execute(
                    "UPDATE adoption_steps SET state = 'settled', updated_at = ?, result = ? "
                    "WHERE step_id = ?",
                    (utc_now(), dumps_canonical({"class_decision": decision}).decode(), step_id),
                )
    finally:
        conn.close()
    return {"family_id": family_id, "decision": decision}


# ----- decisions (T0) ------------------------------------------------------------------------


def decisions_for(
    conn: sqlite3.Connection, root_alias: str, rows: list[AdoptionFile]
) -> dict[str, str]:
    """The owner's latest decision per family, as a path of this generation: the decided path
    when it is still there, else a file with the decided content (a move keeps the decision)."""
    by_path = {r.path: r for r in rows}
    by_sha: dict[str, str] = {}
    for r in rows:
        if r.sha256 and r.entry == "file":
            by_sha.setdefault(r.sha256, r.path)
    # Only each family's latest decision counts (an older one never comes back when the latest
    # no longer applies), and the result is ordered oldest to newest, so where several decided
    # files end up in one family the newest decision wins (review cp17-5-a-r2 R2-1).
    latest: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT family_id, current_path, current_sha256, decided_at FROM adoption_decisions "
        "WHERE root_alias = ? ORDER BY decided_at, decision_id",
        (root_alias,),
    ).fetchall():
        latest.pop(row["family_id"], None)
        latest[row["family_id"]] = row
    out: dict[str, str] = {}
    for fid, row in latest.items():
        path = row["current_path"]
        if path in by_path and (row["current_sha256"] in (None, by_path[path].sha256)):
            out[fid] = path
        elif row["current_sha256"] in by_sha:
            out[fid] = by_sha[row["current_sha256"]]
    return out


def fuse_again(conn: sqlite3.Connection, scan_gen: int) -> list[AdoptionFamily]:
    """Fuse a generation again with the owner's decisions and store the families."""
    from prometheos.adoption import fusion

    scan = tables.require_scan(conn, scan_gen)
    if not scan.config.rank:
        return []  # a scan made without ranking never gets families (review cp17-234-a F12)
    rows = tables.all_files(conn, scan_gen)
    families = fusion.fuse(
        scan_gen,
        rows,
        tables.all_evidence(conn, scan_gen),
        decisions_for(conn, scan.root_alias, rows),
    )
    tables.replace_families(conn, scan_gen, families)
    return families


# ----- the service's operations ---------------------------------------------------------------


def _create_items(store: Any, steps: list[PlanStep], actor: str, rank: int) -> int:
    """Create the work items (proposed actions) of steps that have none, in one transaction
    through the record service; IDs and the request key derive from the steps, so a retry or
    a later plan version never duplicates an item."""
    from prometheos.app.apply import apply_request
    from prometheos.domain.ids import derived_request_id

    conn = store.connect(readonly=True)
    try:
        existing = {
            r[0] for r in conn.execute("SELECT id FROM records WHERE kind = 'action'").fetchall()
        }
        seq = int(conn.execute("SELECT project_seq FROM project_state").fetchone()[0])
    finally:
        conn.close()
    from prometheos.app.outbox import DETECTOR_ACTOR

    ops: list[tuple[str, dict[str, Any]]] = []
    for step in steps:
        action_id = action_id_for(store.project_id, step.step_id)
        if action_id in existing:
            continue
        # A check is PrometheOS's own planned check (its rules derived it), recorded as the
        # detector's so CP20.3's budget rule can admit it; the rest are the caller's proposals.
        by = DETECTOR_ACTOR if is_check(step) else actor
        body = work_item_body(step)
        ops.append((by, {"op": "create", "kind": "action", "id": action_id, "base_rev": 0,
                         "sensitivity": "normal", "occurred_at": None, "body": body}))  # fmt: skip
    if not ops:
        return 0
    batches = []
    for by in dict.fromkeys(b for b, _ in ops):
        mine = [o for b, o in ops if b == by]
        batches += [(by, mine[start : start + 100]) for start in range(0, len(mine), 100)]
    for by, batch in batches:
        request = {
            "schema_version": 1,
            "project_id": store.project_id,
            "request_id": derived_request_id(
                "adoption-plan-items", store.project_id, *[str(o["id"]) for o in batch]
            ),
            "expected_seq": seq,
            "operations": batch,
        }
        response = apply_request(store, dumps_canonical(request), by, max_rank=rank)
        seq = int(response["project_seq"])
    return len(ops)


def action_id_for(project_id: str, step_id: str) -> str:
    from prometheos.domain.ids import derived_request_id

    return "action_" + derived_request_id("adoption-step-item", project_id, step_id)


def _new_version(
    conn: sqlite3.Connection, scan_gen: int, actor: str
) -> tuple[str, int, list[PlanStep]]:
    """Derive and save a plan version, inside the caller's transaction."""
    from prometheos.adoption import assist

    scan = tables.require_scan(conn, scan_gen)
    if scan.state != "complete":
        raise PrometheosError("validation_error", "plan a complete scan", {})
    plan_id = plan_id_for(scan.root_alias)
    steps, omitted = derive(
        scan.root_alias,
        tables.all_files(conn, scan_gen),
        tables.list_families(conn, scan_gen),
        tables.all_evidence(conn, scan_gen),
        assist.tasks(conn, scan_gen)["tasks"],
        settled_ids(conn, plan_id),
        rerun_outcomes(conn, plan_id),
        frozenset(class_decisions(conn, scan_gen)),
    )
    return plan_id, save_version(conn, plan_id, scan_gen, steps, actor, omitted), steps


def _attach_items(store: Any, steps: list[PlanStep], actor: str, rank: int) -> int:
    """Create the steps' work items and link them (idempotent: IDs derive from the steps)."""
    from prometheos.store import db

    created = _create_items(store, steps, actor, rank)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            for step in steps:
                conn.execute(
                    "UPDATE adoption_steps SET action_id = ? WHERE step_id = ?",
                    (action_id_for(store.project_id, step.step_id), step.step_id),
                )
    finally:
        conn.close()
    return created


def create(store: Any, scan_gen: int, actor: str, rank: int) -> dict[str, Any]:
    """A new plan version for the generation's root, its steps and their work items."""
    from prometheos.store import db

    conn = store.connect()
    try:
        with db.write_transaction(conn):
            plan_id, version, steps = _new_version(conn, scan_gen, actor)
    finally:
        conn.close()
    created = _attach_items(store, steps, actor, rank)
    return {
        "plan_id": plan_id,
        "version": version,
        "scan_gen": scan_gen,
        "steps": len(steps),
        "work_items_created": created,
    }


def view(conn: sqlite3.Connection, scan_gen: int) -> dict[str, Any]:
    scan = tables.require_scan(conn, scan_gen)
    plan_id = plan_id_for(scan.root_alias)
    version = latest_version(conn, plan_id)
    rows = step_rows(conn, plan_id)
    latest = conn.execute(
        "SELECT payload FROM adoption_plans WHERE plan_id = ? AND version = ?", (plan_id, version)
    ).fetchone()
    omitted = json.loads(latest[0]).get("omitted_decisions", 0) if latest else 0
    order = {k: i for i, k in enumerate(STEP_KINDS)}
    items = sorted(
        rows.values(),
        key=lambda r: (r["state"] == "superseded", order[r["step"].kind], r["step"].step_id),
    )
    return {
        "plan_id": plan_id,
        "version": version,
        "scan_gen": scan_gen,
        "root_alias": scan.root_alias,
        "omitted_decisions": omitted,
        "steps": [
            {
                "step": r["step"].model_dump(mode="json"),
                "state": r["state"],
                "action_id": r["action_id"],
                "plan_version": r["plan_version"],
                "result": r["result"],
            }
            for r in items
        ],
    }


def report(store: Any, scan_gen: int, step_id: str, result: Any) -> dict[str, Any]:
    from prometheos.store import db

    conn = store.connect()
    try:
        with db.write_transaction(conn):
            scan = tables.require_scan(conn, scan_gen)
            rows_by_step = step_rows(conn, plan_id_for(scan.root_alias))
            row = rows_by_step.get(step_id)
            if row is None:
                raise PrometheosError("not_found", "no such plan step", {"step_id": step_id[:40]})
            step: PlanStep = row["step"]
            if step.actor != "agent":
                raise PrometheosError(
                    "validation_error", "this step is the owner's decision, not a report", {}
                )
            if row["state"] in ("accepted", "settled", "superseded"):
                raise PrometheosError("validation_error", f"the step is {row['state']}", {})
            inventory = {r.path: r for r in tables.all_files(conn, scan_gen)}
            from prometheos.adoption import assist

            # only tasks PrometheOS accepted count (review cp17-5-a-r2 R2-2)
            known = {
                r["assist_id"]: (r["subject"], r["subject_sha256"])
                for r in assist.accepted_results(conn)
            }
            ok, problems, output = validate_result(step, result, inventory, known)
            outcome = (
                rerun_outcome(step, output) if output is not None and step.kind == "rerun" else {}
            )
            stored = {
                "result": output if ok else None,
                "problems": problems,
                "outcome": outcome,
                "reported_at": utc_now(),
            }
            state = "validated" if ok else "rejected"
            conn.execute(
                "UPDATE adoption_steps SET state = ?, result = ?, updated_at = ? WHERE step_id = ?",
                (state, dumps_canonical(stored).decode(), utc_now(), step_id),
            )
    finally:
        conn.close()
    return {"step_id": step_id, "state": state, "problems": problems, "outcome": outcome}


def decide(
    store: Any,
    scan_gen: int,
    family_id: str,
    current: str,
    note: str | None,
    actor: str,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """The owner's choice of a family's current member (T0): stored with its digest, the
    families fused again, the family's resolve step settled."""
    from prometheos.domain.ids import new_request_id
    from prometheos.store import capabilities, db
    from prometheos.store.project import ProjectStore

    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise PrometheosError("validation_error", "a note is 1..2000 characters", {})
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            scan = tables.require_scan(conn, scan_gen)
            family = tables.get_family(conn, scan_gen, family_id)
            if current not in {m.path for m in family.members}:
                raise PrometheosError("validation_error", "choose a member of the family", {})
            if grant is not None:
                capabilities.consume(
                    conn,
                    secret=grant,
                    project_id=ProjectStore.project_id_in(conn),
                    operation="exact_request",
                    digest=grant_digest,
                )
            sha = conn.execute(
                "SELECT sha256 FROM adoption_files WHERE scan_gen = ? AND path = ?",
                (scan_gen, current),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO adoption_decisions (decision_id, root_alias, family_id, current_path, "
                "current_sha256, note, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "dec_" + new_request_id().replace("-", "")[:24],
                    scan.root_alias,
                    family_id,
                    current,
                    sha,
                    note.strip() if note else None,
                    actor[:100],
                    utc_now(),
                ),
            )
            families = fuse_again(conn, scan_gen)
            step_id = step_id_for(plan_id_for(scan.root_alias), "resolve_conflict", family_id)
            conn.execute(
                "UPDATE adoption_steps SET state = 'settled', updated_at = ?, result = ? "
                "WHERE step_id = ?",
                (utc_now(), dumps_canonical({"decision": current}).decode(), step_id),
            )
    finally:
        conn.close()
    decided = next(f for f in families if f.family_id == family_id)
    return decided.model_dump(mode="json")


def accept_baseline(
    store: Any,
    scan_gen: int,
    note: str | None,
    actor: str,
    rank: int,
    *,
    grant: Any = None,
    grant_digest: str | None = None,
) -> dict[str, Any]:
    """The owner accepts the generation: validated results are accepted, a new plan version
    is made (accepted and settled steps are not asked again), and the baseline records every
    family's current member with digests. The grant, the acceptance, the plan version and the
    baseline commit in one transaction; the new version's work items follow (idempotent)."""
    from prometheos.domain.ids import new_request_id
    from prometheos.store import capabilities, db
    from prometheos.store.project import ProjectStore

    if note is not None and not 1 <= len(note.strip()) <= 2000:
        raise PrometheosError("validation_error", "a note is 1..2000 characters", {})
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            scan = tables.require_scan(conn, scan_gen)
            if scan.state != "complete":
                raise PrometheosError("validation_error", "a baseline needs a complete scan", {})
            if grant is not None:
                capabilities.consume(
                    conn,
                    secret=grant,
                    project_id=ProjectStore.project_id_in(conn),
                    operation="exact_request",
                    digest=grant_digest,
                )
            now = utc_now()
            # First bring the plan up to this generation: a result whose inputs changed since it
            # was validated is reopened and so cannot be accepted (review cp17-5-a F2).
            _new_version(conn, scan_gen, actor)
            accepted = sorted(
                sid
                for sid, row in step_rows(conn, plan_id_for(scan.root_alias)).items()
                if row["state"] == "validated"
            )
            for sid in accepted:
                conn.execute(
                    "UPDATE adoption_steps SET state = 'accepted', updated_at = ? "
                    "WHERE step_id = ?",
                    (now, sid),
                )
            plan_id, version, steps = _new_version(conn, scan_gen, actor)
            payload = baseline_payload(conn, scan_gen, plan_id, version)
            payload["accepted_steps"] = accepted
            payload["decisions"] = decisions_for(
                conn, scan.root_alias, tables.all_files(conn, scan_gen)
            )
            payload["note"] = note.strip() if note else None
            digest = canonical_sha256(payload)
            baseline_id = "bl_" + new_request_id().replace("-", "")[:24]
            conn.execute(
                "INSERT INTO adoption_baselines (baseline_id, scan_gen, plan_id, plan_version, "
                "accepted_at, accepted_by, sha256, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    baseline_id,
                    scan_gen,
                    plan_id,
                    version,
                    now,
                    actor[:100],
                    digest,
                    dumps_canonical(payload).decode(),
                ),
            )
    finally:
        conn.close()
    _attach_items(store, steps, actor, rank)
    return {
        "baseline_id": baseline_id,
        "sha256": digest,
        "scan_gen": scan_gen,
        "plan": {"plan_id": plan_id, "version": version},
        "accepted_steps": len(accepted),
    }


def baseline(conn: sqlite3.Connection, baseline_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM adoption_baselines WHERE baseline_id = ?", (baseline_id,)
    ).fetchone()
    if row is None:
        raise PrometheosError("not_found", "no such baseline", {"baseline_id": baseline_id[:40]})
    return {
        "baseline_id": row["baseline_id"],
        "scan_gen": row["scan_gen"],
        "plan_id": row["plan_id"],
        "plan_version": row["plan_version"],
        "accepted_at": row["accepted_at"],
        "accepted_by": row["accepted_by"],
        "sha256": row["sha256"],
        "payload": json.loads(row["payload"]),
    }


def latest_baseline(conn: sqlite3.Connection, root_alias: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT b.baseline_id FROM adoption_baselines b JOIN adoption_scans s "
        "ON s.scan_gen = b.scan_gen WHERE s.root_alias = ? ORDER BY b.accepted_at DESC LIMIT 1",
        (root_alias,),
    ).fetchone()
    return None if row is None else baseline(conn, row[0])


def baselines(conn: sqlite3.Connection, root_alias: str | None = None) -> dict[str, Any]:
    """Accepted baselines, newest first (without payloads)."""
    rows = conn.execute(
        "SELECT b.baseline_id, b.scan_gen, b.plan_version, b.accepted_at, b.sha256, "
        "s.root_alias FROM adoption_baselines b JOIN adoption_scans s ON s.scan_gen = b.scan_gen "
        "WHERE ? IS NULL OR s.root_alias = ? ORDER BY b.accepted_at DESC, b.baseline_id",
        (root_alias, root_alias),
    ).fetchall()
    items = [
        {"baseline_id": r["baseline_id"], "root_alias": r["root_alias"], "scan_gen": r["scan_gen"],
         "plan_version": r["plan_version"], "accepted_at": r["accepted_at"], "sha256": r["sha256"]}
        for r in rows[:200]
    ]  # fmt: skip
    return {"baselines": items, "total": len(rows)}
