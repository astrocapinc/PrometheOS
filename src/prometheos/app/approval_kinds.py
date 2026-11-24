"""What each protected operation stores as its exact plan and how ``approval resume`` runs
it (A02, CP10.2; decision 0016).

Every kind builds a canonical plan from the agent's parameters *and the current state* (the
"base" the owner approves: a plan digest, a revision, a slot state), names the capability
operation a grant needs and the digest that grant binds, and executes the stored plan once
with the server-held grant. The executor re-derives the base and refuses a changed one
(``revision_conflict``: the agent submits a new request). Where the effect is inside the
project store, the grant is consumed in the same transaction as the effect; where it is
outside (a download, a model folder, the skills catalogue, a recording, a Git push, a
provider call), the grant's use commits first, so the effect happens at most once.

Parameters arrive from transports, so each kind parses them with a closed model. Text in
the parameters is data: it becomes part of the plan the owner reads, never an instruction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError

from prometheos.app import approvals, reads
from prometheos.app.authority import OwnerAct
from prometheos.domain.canonical import (
    CanonicalJSONError,
    canonical_sha256,
    dumps_canonical,
    loads_internal,
)
from prometheos.domain.models import JsonObjectInput, PrometheosError, StrictModel, parse_model
from prometheos.store import capabilities
from prometheos.store.operational import ApprovalRequest
from prometheos.store.project import ProjectStore

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
RecordIdText = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class JobResolveParams(StrictModel):
    job_id: RecordIdText
    due_utc: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    resolution: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class EventAcknowledgeParams(StrictModel):
    event_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=50
    )
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class AdoptionDecideParams(StrictModel):
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    family_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    current: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class AdoptionClassifyParams(StrictModel):
    """A06.1 (CP20.4): the owner's class decision for a family."""

    scan_gen: int = Field(ge=1, le=2**53 - 1)
    family_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    decision: Literal["variant", "noise", "split", "revoked"]
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class AdoptionOutlineParams(StrictModel):
    """A06.1 (CP20.4): the owner's one confirmation of a scan's outline, with corrections."""

    scan_gen: int = Field(ge=1, le=2**53 - 1)
    proposal_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    edits: dict[str, Any] = Field(default_factory=dict)
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


WorkIdText = Annotated[str, StringConstraints(pattern=r"^work_[0-9a-f-]{36}$")]


class LibraryResolveParams(StrictModel):
    """CP11 S11.2 (decision 0062): the owner's choice for a duplicate or version candidate."""

    work_a: WorkIdText
    work_b: WorkIdText
    choice: Annotated[str, StringConstraints(pattern=r"^(merge|distinct|versions)$")]
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class LibraryUndoParams(StrictModel):
    work_a: WorkIdText
    work_b: WorkIdText
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class AdoptionBaselineParams(StrictModel):
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class ProjectRootAddParams(StrictModel):
    """CP11 S11.4b (decision 0066): a research folder to add (owner act)."""

    alias: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


ZoteroKeyText = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{8}$")]


class ZoteroCommitParams(StrictModel):
    """CP11 S11.5 (decision 0070 section 7): a reviewed Zotero plan to commit (owner act)."""

    plan_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    item_keys: list[ZoteroKeyText] | None = Field(default=None, min_length=1, max_length=200)


class AdoptionExportParams(StrictModel):
    baseline_id: Annotated[str, StringConstraints(pattern=r"^bl_[0-9a-f]{24}$")]
    root_alias: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    folder: Annotated[str, StringConstraints(max_length=512)] = ""


class PushParams(StrictModel):
    paper_id: RecordIdText
    digest: Sha


class PurgeParams(StrictModel):
    plan_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class ProviderRunParams(StrictModel):
    purpose: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    meeting_id: RecordIdText | None = None
    context_budget: int = Field(default=12000, ge=1000, le=200_000)
    digest: Sha


class AiPolicyGrantParams(StrictModel):
    policy: JsonObjectInput
    binding_sha256: Sha


class RootSensitivityParams(StrictModel):
    root_alias: Name
    sensitivity: Literal["normal", "restricted", "private"]


class AiAcceptParams(StrictModel):
    task_kind: Name
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    subject: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    result_keys: list[Sha] | None = None


class AiRunParams(StrictModel):
    task_kind: Name
    scan_gen: int = Field(ge=1, le=2**53 - 1)
    paths: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=500
    )


class ProviderConfigureParams(StrictModel):
    config: JsonObjectInput
    revision: int = Field(ge=0, le=2**53 - 1)


class DigestParams(StrictModel):
    digest: Sha


class ModelInstallParams(StrictModel):
    model: Name = "faster-whisper-base"
    plan_sha256: Sha


class ModelRemoveParams(StrictModel):
    model: Name = "faster-whisper-base"


class SkillTrustParams(StrictModel):
    ref: Name


class BackupParams(StrictModel):
    """A backup request takes no parameters: the archive is written into the store's own
    ``exports/`` folder, exactly as ``backup create`` does without ``--out`` (CP10.5)."""


class SkillProjectionParams(StrictModel):
    ref: Name
    host: str
    root_alias: str
    folder: str | None = None
    plan_sha256: Sha
    record_in_project: bool = False
    resolve: str | None = None


class SkillRemovalParams(StrictModel):
    name: Name
    host: str
    root_alias: str
    folder: str | None = None
    plan_sha256: Sha
    record_in_project: bool = False


class RecordingParams(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    device_ids: list[Name] = Field(min_length=1, max_length=2)
    sensitivity: Annotated[str, StringConstraints(pattern=r"^(normal|restricted|private)$")] = (
        "restricted"
    )
    segment_seconds: int = Field(default=30, ge=5, le=300)
    max_seconds: int = Field(default=3600, ge=10, le=4 * 3600)


@dataclass(frozen=True)
class Built:
    plan: dict[str, Any]
    summary: list[str]


@dataclass(frozen=True)
class Kind:
    operation: str
    scope: str  # capability operation a grant issues
    params: type[StrictModel] | None
    build: Callable[[Any, str, ProjectStore, reads.Caller, Any], Built]
    digest: Callable[[dict[str, Any]], str]  # what the grant binds, from the stored plan
    execute: Callable[
        [
            Any,
            str,
            ProjectStore,
            ApprovalRequest,
            capabilities.HeldGrant,
            reads.Caller,
            dict[str, Any],
        ],
        dict[str, Any],
    ]


def caller_for(row: ApprovalRequest, actor: str) -> reads.Caller:
    """The caller an approved plan runs as: the rank that requested it."""
    return reads.Caller(actor=actor, scope="owner" if row.caller_rank >= 2 else "agent")


OWNER_VIEW = reads.Caller(actor="approval-owner-review", scope="owner")
"""The owner's visibility, used only to compute what the owner reviews (never returned to the
requester: the plan stores only the digest of that view)."""

START_WINDOW_SECONDS = 600
"""A granted recording must start within this many seconds of the owner's grant (F1)."""

CAPTURE_HOSTS = frozenset({"hub-owner", "cli-interactive"})
"""Where a granted recording may capture: in the owner's paired hub (visible banner with a stop
control) or an interactive terminal at the computer. Never in an MCP or agent HTTP process."""


def _plan_digest(plan: dict[str, Any]) -> str:
    return canonical_sha256(plan)


def _changed(what: str, **details: Any) -> PrometheosError:
    return PrometheosError(
        "revision_conflict",
        f"{what} changed since the approval was requested; submit a new request",
        details,
    )


# ----- apply (agent_or_grant; built inside apply_request) ------------------------------------


def _apply_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                   grant: capabilities.HeldGrant, caller: reads.Caller,
                   options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.app.apply import apply_request

    raw = dumps_canonical({**row.plan, "request_id": row.request_id})
    try:
        result: dict[str, Any] = apply_request(
            store, raw, svc.actor, max_rank=row.caller_rank, capability=grant
        )
    except PrometheosError as exc:
        if exc.code == "revision_conflict" and "expected_seq" in exc.details:
            raise PrometheosError(
                "revision_conflict",
                f"the project changed since approval request {row.id} was made (sequence "
                f"{exc.details['expected_seq']}, now {exc.details.get('project_seq')}); the grant "
                "binds that exact state and stays unused: submit a new request",
                {"approval_request_id": row.id, **exc.details},
            ) from exc
        raise
    return result


def _no_build(*_: Any) -> Built:
    raise PrometheosError("validation_error", "apply requests are made with apply", {})


# ----- job resolve -----------------------------------------------------------------------------


def _job_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
               p: JobResolveParams) -> Built:  # fmt: skip
    from prometheos.app import jobs

    jobs.resolve_digest(project_id, p.job_id, p.due_utc, p.resolution, p.reason)  # validates
    slots = svc.job_slots(project_id, caller, p.job_id)["slots"]
    slot = next((s for s in slots if s.get("due_utc") == p.due_utc), None)
    if slot is None:
        raise PrometheosError("not_found", "slot not found", {"due_utc": p.due_utc})
    if slot.get("state") != "unknown":
        raise PrometheosError(
            "validation_error",
            f"only an unknown slot can be resolved; this one is {slot.get('state')}",
            {"due_utc": p.due_utc},
        )
    plan = {
        "op": "job_resolve_slot",
        "project_id": project_id,
        "job_id": p.job_id,
        "due_utc": p.due_utc,
        "resolution": p.resolution,
        "reason": p.reason.strip(),
    }
    return Built(plan, [f"record the unknown job slot {p.due_utc} as {p.resolution}"])


def _job_digest(plan: dict[str, Any]) -> str:
    from prometheos.app import jobs

    return jobs.resolve_digest(
        plan["project_id"], plan["job_id"], plan["due_utc"], plan["resolution"], plan["reason"]
    )


def _job_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                 grant: capabilities.HeldGrant, caller: reads.Caller,
                 options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    svc.job_slots(project_id, caller, plan["job_id"])  # still visible at the requester's rank
    result: dict[str, Any] = svc.job_resolve_slot(
        project_id, caller, plan["job_id"], plan["due_utc"], plan["resolution"], plan["reason"],
        capability=grant,
    )  # fmt: skip
    return result


# ----- CP18.3: acknowledging research events (decision 0035) ---------------------------------


def _ack_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
               p: EventAcknowledgeParams) -> Built:  # fmt: skip
    from prometheos.app import events

    found = svc._read(project_id, lambda c, d: events.open_events(c, caller, p.event_ids))
    plan = {
        "op": "event_acknowledge",
        "project_id": project_id,
        "event_ids": sorted(p.event_ids),
        "note": p.note,
        "events": [
            {"event_id": e.event_id, "type": e.type, "reason": events.reason(e)} for e in found
        ],
    }
    noun = "event" if len(found) == 1 else "events"
    return Built(plan, [f"acknowledge {len(found)} {noun}: {e.type}" for e in found][:50])


def _ack_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                 grant: capabilities.HeldGrant, caller: reads.Caller,
                 options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.app import events

    plan: dict[str, Any] = row.plan
    result: dict[str, Any] = events.acknowledge(
        store, list(plan["event_ids"]), plan["note"],
        f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


# ----- CP17.5: adoption decisions and baselines (decision 0043) ------------------------------


def _decide_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                  p: AdoptionDecideParams) -> Built:  # fmt: skip
    family = svc.adoption_family(project_id, caller, p.scan_gen, p.family_id)
    members = {m["path"]: m for m in family["members"]}
    if p.current not in members:
        raise PrometheosError("validation_error", "choose a member of the family", {})
    plan = {
        "op": "adoption_decide",
        "project_id": project_id,
        "scan_gen": p.scan_gen,
        "family_id": p.family_id,
        "key": family["key"],
        "current": p.current,
        "was": family["current"],
        "band": family["band"],
        "note": p.note,
    }
    return Built(plan, [f"make {p.current} the current file of family {family['key']}"])


def _decide_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                    grant: capabilities.HeldGrant, caller: reads.Caller,
                    options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.adoption import plan as adoption_plan

    plan: dict[str, Any] = row.plan
    result: dict[str, Any] = adoption_plan.decide(
        store, plan["scan_gen"], plan["family_id"], plan["current"], plan["note"],
        f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


_CLASS_TEXT = {
    "variant": "each file is current in its own context",
    "noise": "none of these files matters",
    "split": "these files are not one family",
    "revoked": "undo the earlier class decision",
}


def _classify_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                    p: AdoptionClassifyParams) -> Built:  # fmt: skip
    family = svc.adoption_family(project_id, caller, p.scan_gen, p.family_id)
    plan = {
        "op": "adoption_classify",
        "project_id": project_id,
        "scan_gen": p.scan_gen,
        "family_id": p.family_id,
        "key": family["key"],
        "members": sorted(m["path"] for m in family["members"]),
        "decision": p.decision,
        "note": p.note,
    }
    return Built(plan, [f"family {family['key']}: {p.decision} ({_CLASS_TEXT[p.decision]})"])


def _classify_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                      grant: capabilities.HeldGrant, caller: reads.Caller,
                      options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.adoption import plan as adoption_plan

    plan: dict[str, Any] = row.plan
    result: dict[str, Any] = adoption_plan.decide_class(
        store, plan["scan_gen"], plan["family_id"], plan["decision"], plan["note"],
        f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


def _outline_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                   p: AdoptionOutlineParams) -> Built:  # fmt: skip
    seen = svc.adoption_outline_preview(project_id, caller, p.scan_gen, p.proposal_sha256, p.edits)
    plan = {
        "op": "adoption_outline_confirm",
        "project_id": project_id,
        "scan_gen": p.scan_gen,
        "proposal_sha256": p.proposal_sha256,
        "edits": p.edits,
        "edits_sha256": seen["edits_sha256"],
        "note": p.note,
    }
    kept = seen["kept"]
    return Built(plan, [
        f"confirm the adoption outline of scan {p.scan_gen}: {kept['track']} tracks, "
        f"{kept['deliverable']} deliverables, {kept['question']} questions "
        f"({seen['dropped']} items left out)",
        f"records created: {seen['records_created']}, reused: {seen['records_reused']}; "
        f"links added: {seen['relations']}",
    ])  # fmt: skip


def _outline_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                     grant: capabilities.HeldGrant, caller: reads.Caller,
                     options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.adoption import outline as adoption_outline

    plan: dict[str, Any] = row.plan
    root = svc._adoption_root(project_id, plan["scan_gen"])
    result: dict[str, Any] = adoption_outline.confirm(
        store, root, plan["scan_gen"], plan["proposal_sha256"],
        adoption_outline.Edits.model_validate(plan["edits"]), plan["note"],
        f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


# ----- CP11 S11.2: duplicate and version candidates (decision 0062) -----------------------------


def _resolve_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                   p: LibraryResolveParams) -> Built:  # fmt: skip
    from prometheos.library import dedup

    plan = svc._read(
        project_id, lambda c, d: dedup.plan_of(c, caller, p.work_a, p.work_b, p.choice)
    )
    plan = {**plan, "project_id": project_id, "note": p.note}
    what = {
        "merge": f"merge {plan['merged']} into {plan['into']} (merged_into; nothing else moves)",
        "distinct": f"keep {plan['work_a']} and {plan['work_b']} separate (remembered)",
        "versions": f"relate {plan['preprint']} (preprint) to {plan['published']} as version_of",
    }[p.choice]
    return Built(plan, [what])


def _resolve_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                     grant: capabilities.HeldGrant, caller: reads.Caller,
                     options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.domain.ids import derived_request_id
    from prometheos.library import dedup

    plan: dict[str, Any] = dict(row.plan)
    work_plan = {k: v for k, v in plan.items() if k not in ("project_id", "note")}
    result: dict[str, Any] = dedup.execute(
        store, work_plan, plan.get("note"), f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
        request_id=derived_request_id("library-dedup-resolve", project_id, row.id),
    )  # fmt: skip
    return result


def _undo_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                p: LibraryUndoParams) -> Built:  # fmt: skip
    from prometheos.library import dedup

    plan = svc._read(project_id, lambda c, d: dedup.plan_undo(c, caller, p.work_a, p.work_b))
    plan = {**plan, "project_id": project_id, "note": p.note}
    return Built(plan, [f"undo {plan['undo']} for {plan['work_a']} and {plan['work_b']}"])


def _undo_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                  grant: capabilities.HeldGrant, caller: reads.Caller,
                  options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.domain.ids import derived_request_id
    from prometheos.library import dedup

    plan: dict[str, Any] = dict(row.plan)
    undo_plan = {k: v for k, v in plan.items() if k not in ("project_id", "note")}
    result: dict[str, Any] = dedup.execute_undo(
        store, undo_plan, plan.get("note"), f"{row.decided_by or 'owner'} via {row.id}"[:100],
        grant=grant, grant_digest=_plan_digest(plan),
        request_id=derived_request_id("library-dedup-undo", project_id, row.id),
    )  # fmt: skip
    return result


def _root_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                p: ProjectRootAddParams) -> Built:  # fmt: skip
    """An agent's request: checked without touching the disk, one refusal for every location
    rule (S11.4b review F5). The summary names the folder without an absolute path, so the hub
    shows the owner what is asked (F1): a base placeholder, the folders below it, warnings."""
    from prometheos.app import roots

    plan = svc.project_root_request_plan(project_id, p.alias, p.path)
    folder = plan["folder_view"]
    summary = [f"add the research folder {plan['alias']}: {roots.folder_text(folder)}"]
    summary += [f"warning: {w}" for w in folder["warnings"]]
    return Built(plan, summary)


def _root_review(svc: Any, project_id: str, row: ApprovalRequest) -> dict[str, Any]:
    """The owner's review: the full location rules now (the folder exists, is not a link to
    another folder, the settings did not change); only the owner learns the reason."""
    return dict(svc.project_root_review(project_id, dict(row.plan)))


def _root_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                  grant: capabilities.HeldGrant, caller: reads.Caller,
                  options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.app import roots
    from prometheos.domain.ids import derived_request_id

    plan: dict[str, Any] = dict(row.plan)
    result: dict[str, Any] = roots.execute_add(
        store, plan, f"{row.decided_by or 'owner'} via {row.id}"[:100], home=svc.home,
        request_id=derived_request_id("project-root-add", project_id, row.id),
        grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


def _zotero_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                  p: ZoteroCommitParams) -> Built:  # fmt: skip
    """An agent's request to bring items of the owner's Zotero library into the project: the
    exact plan names the items (key, version, digest), so the grant binds exactly those."""
    from prometheos.library import zotero

    plan = zotero.plan_for(store, p.plan_id, caller.rank)
    keys = sorted(dict.fromkeys(p.item_keys)) if p.item_keys else None
    chosen = zotero.selected(plan, keys)
    if not chosen:
        raise PrometheosError("validation_error", "the plan has nothing to import", {})
    items = [
        {"key": i["key"], "version": i["version"], "sha256": i["sha256"], "action": i["action"]}
        for _, i in chosen
    ]
    built = {
        "op": "zotero_import_commit",
        "project_id": project_id,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "scope": plan["scope"],
        "sensitivity": plan["sensitivity"],
        "item_keys": keys,
        "items": items,
    }
    titles = [str(i.get("title") or i["key"])[:80] for _, i in chosen[:3]]
    more = f" and {len(chosen) - 3} more" if len(chosen) > 3 else ""
    return Built(built, [
        f"read {len(items)} item(s) from the owner's local Zotero library into the project "
        f"({plan['scope']['kind']}): " + "; ".join(titles) + more
    ])  # fmt: skip


def _zotero_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                    grant: capabilities.HeldGrant, caller: reads.Caller,
                    options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.library import zotero

    plan: dict[str, Any] = dict(row.plan)
    current = zotero.plan_for(store, plan["plan_id"], caller.rank)
    if current["plan_sha256"] != plan["plan_sha256"]:
        raise _changed("the Zotero plan")
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.zotero_import_commit(
        project_id, plan["plan_id"], caller, item_keys=plan["item_keys"], authority=act,
        approval_request_id=row.id,
    )  # fmt: skip
    return result


def _baseline_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                    p: AdoptionBaselineParams) -> Built:  # fmt: skip
    current = svc.adoption_plan(project_id, caller, p.scan_gen)
    families = svc.adoption_families(project_id, caller, p.scan_gen, limit=1)
    validated = sorted(s["step"]["step_id"] for s in current["steps"] if s["state"] == "validated")
    plan = {
        "op": "adoption_baseline_accept",
        "project_id": project_id,
        "scan_gen": p.scan_gen,
        "root_alias": current["root_alias"],
        "plan_version": current["version"],
        "accept_steps": validated,
        "families": families["total"],
        "note": p.note,
    }
    return Built(plan, [
        f"accept scan {p.scan_gen} of {current['root_alias']} as the adoption baseline "
        f"({families['total']} families, {len(validated)} validated results)"
    ])  # fmt: skip


def _baseline_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                      grant: capabilities.HeldGrant, caller: reads.Caller,
                      options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.adoption import plan as adoption_plan

    plan: dict[str, Any] = row.plan
    current = svc.adoption_plan(project_id, caller, plan["scan_gen"])
    validated = sorted(s["step"]["step_id"] for s in current["steps"] if s["state"] == "validated")
    if current["version"] != plan["plan_version"] or validated != plan["accept_steps"]:
        raise _changed("the adoption plan", digest=None)
    result: dict[str, Any] = adoption_plan.accept_baseline(
        store, plan["scan_gen"], plan["note"], f"{row.decided_by or 'owner'} via {row.id}"[:100],
        0, grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


def _export_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                  p: AdoptionExportParams) -> Built:  # fmt: skip
    planned = svc.adoption_export_markdown_plan(
        project_id, caller, p.baseline_id, p.root_alias, p.folder
    )
    plan = {"op": "adoption_export_markdown", "project_id": project_id, **planned,
            "folder": p.folder}  # fmt: skip
    verb = {"create": "create", "replace": "update", "unchanged": "rewrite"}[planned["action"]]
    return Built(plan, [f"{verb} the note {planned['note']} in {p.root_alias} "
                        f"(baseline {p.baseline_id}; text outside its block is kept)"])  # fmt: skip


def _export_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                    grant: capabilities.HeldGrant, caller: reads.Caller,
                    options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    now = svc.adoption_export_markdown_plan(
        project_id, caller, plan["baseline_id"], plan["root_alias"], plan["folder"]
    )
    if (now["text_sha256"], now["previous_sha256"]) != (
        plan["text_sha256"],
        plan["previous_sha256"],
    ):
        raise _changed("the note or the baseline")
    result: dict[str, Any] = svc.adoption_export_markdown(
        project_id, plan["baseline_id"], plan["root_alias"], plan["folder"],
        authority=None, grant=grant, grant_digest=_plan_digest(plan),
    )  # fmt: skip
    return result


# ----- Overleaf push ---------------------------------------------------------------------------


def _push_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                p: PushParams) -> Built:  # fmt: skip
    preview = svc.overleaf_push_preview(project_id, caller, p.paper_id)
    if preview["digest"] != p.digest:
        raise _changed("the push plan", digest=preview["digest"])
    if not preview["applicable"]:
        raise PrometheosError(
            "revision_conflict", preview["plan"].get("explanation", "nothing to push"), {}
        )
    shown = preview["plan"]
    # The owner approves the full push (every file, restricted ones named); the agent's plan
    # binds a keyed digest of that owner view without revealing it (review R2-1).
    owner_view = _push_owner_view(svc, project_id, p.paper_id)
    plan = {
        "op": "overleaf_push",
        "paper_id": p.paper_id,
        "digest": p.digest,
        "push_plan": shown,
        "owner_view_mac": _binding(svc, _push_binding(owner_view)),
    }
    commits = shown.get("commits") or []
    return Built(
        plan,
        [f"push {len(commits)} commit(s) to Overleaf branch {shown.get('branch')} (external "
         "transmission; never forced)"],
    )  # fmt: skip


def _binding(svc: Any, value: object) -> str:
    return approvals.keyed_digest(approvals.binding_key(svc.home), value)


def _push_binding(view: dict[str, Any]) -> dict[str, Any]:
    """What the grant binds of the owner's push view (review R2-2): what is transmitted (the
    CP9 digest of kind, branch, heads and commit OIDs, and the files those commits change)
    and which of them are restricted. Uncommitted local edits are never pushed, so an
    unrelated edit after the grant does not void it."""
    plan = view["plan"]
    return {
        "digest": view["digest"],
        "files": plan.get("files"),
        "restricted_files": plan.get("restricted_files") or [],
    }


def _push_owner_view(svc: Any, project_id: str, paper_id: str) -> dict[str, Any]:
    owner = svc.overleaf_push_preview(project_id, OWNER_VIEW, paper_id)
    view: dict[str, Any] = owner["plan"]
    return {"digest": owner["digest"], "plan": view}


def _push_review(svc: Any, project_id: str, row: ApprovalRequest) -> dict[str, Any]:
    view = _push_owner_view(svc, project_id, str(row.plan["paper_id"]))
    return {
        "kind": "overleaf_push",
        "owner_view": view,
        "restricted_files": view["plan"].get("restricted_files") or [],
        "matches_plan": _binding(svc, _push_binding(view)) == row.plan.get("owner_view_mac"),
    }


def _push_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                  grant: capabilities.HeldGrant, caller: reads.Caller,
                  options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    view = _push_owner_view(svc, project_id, str(row.plan["paper_id"]))
    if _binding(svc, _push_binding(view)) != row.plan.get("owner_view_mac"):
        raise _changed("the push the owner reviewed")
    result: dict[str, Any] = svc.overleaf_push(
        project_id, caller, row.plan["paper_id"], row.plan["digest"], grant
    )
    return result


# ----- retention purge commit ------------------------------------------------------------------


def _purge_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                 p: PurgeParams) -> Built:  # fmt: skip
    conn = store.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT plan FROM purge_plans WHERE plan_id = ?", (p.plan_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PrometheosError("not_found", "purge plan does not exist", {"plan_id": p.plan_id})
    summary = loads_internal(row["plan"])
    # Only the plan's identity and digests: the closure names records an agent may not see. The
    # owner reviews the closure itself (owner_review), whose digest the plan binds.
    plan = {
        "op": "retention_purge_commit",
        "plan_id": p.plan_id,
        "purge_plan_sha256": summary["plan_sha256"],
        "owner_view_mac": _binding(svc, _purge_owner_view(summary)),
    }
    return Built(plan, ["purge the records of an owner purge plan (irreversible)"])


def _purge_owner_view(summary: dict[str, Any]) -> dict[str, Any]:
    keep = ("closure", "payloads", "media", "limitations", "expected_seq")
    return {k: summary.get(k) for k in keep}


def _purge_summary(store: ProjectStore, plan_id: str) -> dict[str, Any] | None:
    conn = store.connect(readonly=True)
    try:
        row = conn.execute("SELECT plan FROM purge_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    found: dict[str, Any] | None = None if row is None else loads_internal(row["plan"])
    return found


def _purge_review(svc: Any, project_id: str, row: ApprovalRequest) -> dict[str, Any]:
    summary = _purge_summary(svc.open(project_id), str(row.plan["plan_id"]))
    view = _purge_owner_view(summary) if summary else None
    return {
        "kind": "retention_purge_commit",
        "owner_view": view,
        "counts": None
        if view is None
        else {k: len(view.get(k) or []) for k in ("closure", "payloads", "media")},
        "matches_plan": view is not None and _binding(svc, view) == row.plan.get("owner_view_mac"),
    }


def _purge_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                   grant: capabilities.HeldGrant, caller: reads.Caller,
                   options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    summary = _purge_summary(store, str(row.plan["plan_id"]))
    if summary is None or _binding(svc, _purge_owner_view(summary)) != row.plan.get(
        "owner_view_mac"
    ):
        raise _changed("the purge plan the owner reviewed")
    done = svc.purge_commit(project_id, row.plan["plan_id"], grant)
    # Only the receipt (counts, sequence): the commit's per-record results name purged records,
    # restricted ones included, which the requesting agent may not see.
    return {"purge_receipt": done["purge_receipt"]}


# ----- provider run and configure --------------------------------------------------------------


def _run_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
               p: ProviderRunParams) -> Built:  # fmt: skip
    shown = svc.provider_preview(
        project_id, caller, purpose=p.purpose, meeting_id=p.meeting_id,
        context_budget=p.context_budget, include_restricted=False,
    )  # fmt: skip
    if shown["digest"] != p.digest:
        raise _changed("what would be sent", digest=shown["digest"])
    if shown["blockers"]:
        code = "policy_denied" if shown["policy"] == "no-ai" else "validation_error"
        raise PrometheosError(code, shown["blockers"][0], {"blockers": shown["blockers"]})
    plan = {
        "op": "provider_run",
        "purpose": p.purpose,
        "meeting_id": p.meeting_id,
        "context_budget": p.context_budget,
        "include_restricted": False,
        "approved_digest": p.digest,
        "route": shown["route"],
        "endpoint_host": shown["endpoint_host"],
        "model": shown["model"],
        "transmits": shown["transmits"],
        "cost": {"estimate_upper_bound_usd": shown["cost"]["estimate_upper_bound_usd"]},
    }
    return Built(
        plan,
        [f"send one {p.purpose} request to the {shown['route']} provider at "
         f"{shown['endpoint_host'] or 'its endpoint'} (egress)"],
    )  # fmt: skip


def _run_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                 grant: capabilities.HeldGrant, caller: reads.Caller,
                 options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    # The run that has waited on this request since it was made continues (decision 0017):
    # the grant is spent in the same transaction that queues it.
    result: dict[str, Any] = svc.provider_run(
        project_id, purpose=plan["purpose"], meeting_id=plan["meeting_id"],
        context_budget=plan["context_budget"], include_restricted=False,
        approved_digest=plan["approved_digest"], capability=grant, caller=caller,
        approval_request_id=row.id,
    )  # fmt: skip
    return result


def _configure_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                     p: ProviderConfigureParams) -> Built:  # fmt: skip
    shown = svc.provider_configure_preview(project_id, p.config, p.revision)
    if shown["stale"]:
        raise _changed("the provider configuration", revision=shown["current_revision"])
    before = _routing(svc.provider_status(project_id, caller)["config"])
    after = _routing(shown["config"])
    new_host = bool(after["credential_ref"]) and after["endpoint_host"] != before["endpoint_host"]
    plan = {
        "op": "provider_configure",
        "config": shown["config"],
        "revision": p.revision,
        "digest": shown["digest"],
        "changes": shown["changes"],
        # What the owner must see (credential references only, never values).
        "before": before,
        "after": after,
        "credential_to_new_host": new_host,
    }
    lines = [
        f"{field}: {before[field]} -> {after[field]}"
        for field in ("policy", "route", "endpoint_host", "model", "credential_ref")
        if before[field] != after[field]
    ]
    if new_host:
        lines.append(
            f"WARNING: the stored credential {after['credential_ref']} would be sent to a new "
            f"host ({after['endpoint_host']})"
        )
    return Built(plan, lines or ["re-save the provider settings (no routing change)"])


def _routing(config: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlparse

    endpoint = config.get("endpoint") or ""
    return {
        "policy": config.get("policy"),
        "route": config.get("route"),
        "endpoint": endpoint or None,
        "endpoint_host": (urlparse(endpoint).hostname or None) if endpoint else None,
        "model": config.get("model"),
        "credential_ref": config.get("credential_ref"),
    }


def _configure_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                       grant: capabilities.HeldGrant, caller: reads.Caller,
                       options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    result: dict[str, Any] = svc.provider_configure(
        project_id, plan["config"], plan["revision"], grant
    )
    return result


# ----- the intelligence layer (A07, CP22.4; decision 0094) ----------------------------------------


def _ai_policy_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                     p: AiPolicyGrantParams) -> Built:  # fmt: skip
    shown = svc.ai_policy_preview(project_id, p.policy)
    if shown["binding_sha256"] != p.binding_sha256:
        raise _changed("the standing policy or the provider configuration",
                       binding_sha256=shown["binding_sha256"])  # fmt: skip
    plan = {
        "op": "ai_policy_grant",
        "base": svc.ai_policy_base(project_id),
        "policy": shown["policy"],
        "policy_sha256": shown["policy_sha256"],
        "provider_revision": shown["provider_revision"],
        "binding_sha256": shown["binding_sha256"],
        "replaces": shown["replaces"],
    }
    return Built(plan, ["grant the standing policy of the intelligence layer", *shown["summary"]])


def _ai_policy_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                       grant: capabilities.HeldGrant, caller: reads.Caller,
                       options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    shown = svc.ai_policy_preview(project_id, plan["policy"])
    if shown["binding_sha256"] != plan["binding_sha256"]:
        raise _changed("the standing policy or the provider configuration")
    if svc.ai_policy_base(project_id) != plan["base"]:
        # a grant, change or revocation since the request (review cp22-a F6)
        raise _changed("the standing policy")
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.ai_policy_grant(
        project_id, plan["policy"], plan["binding_sha256"], authority=act
    )
    return result


def _root_sensitivity_build(svc: Any, project_id: str, store: ProjectStore,
                            caller: reads.Caller, p: RootSensitivityParams) -> Built:  # fmt: skip
    roots = svc.ai_root_sensitivity_show(project_id)["roots"]
    current = next((r for r in roots if r["root_alias"] == p.root_alias), None)
    if current is None:
        raise PrometheosError("policy_denied", "research root alias is not approved", {})
    folder = str(svc.research_root(project_id, p.root_alias).resolve())
    plan = {
        "op": "root_sensitivity_set",
        "root_alias": p.root_alias,
        "folder": folder,
        "sensitivity": p.sensitivity,
        "was": current["sensitivity"],
    }
    text = (
        f"lower the sensitivity of research root {p.root_alias} ({folder}) from "
        f"{current['sensitivity']} to {p.sensitivity}: files under it may then be sent to the "
        "intelligence layer's provider"
    )
    return Built(plan, [text])


def _root_sensitivity_execute(svc: Any, project_id: str, store: ProjectStore,
                              row: ApprovalRequest, grant: capabilities.HeldGrant,
                              caller: reads.Caller,
                              options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    if str(svc.research_root(project_id, plan["root_alias"]).resolve()) != plan["folder"]:
        raise _changed("the research root's folder")
    roots = svc.ai_root_sensitivity_show(project_id)["roots"]
    now = next((r["sensitivity"] for r in roots if r["root_alias"] == plan["root_alias"]), None)
    if now != plan["was"]:
        raise _changed("the research root's sensitivity", now=now)  # review cp22-a F6
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.ai_root_sensitivity_set(
        project_id, plan["root_alias"], plan["sensitivity"], authority=act
    )
    return result


def _ai_accept_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                     p: AiAcceptParams) -> Built:  # fmt: skip
    shown = svc.ai_accept_preview(project_id, p.task_kind, p.scan_gen, p.subject)
    # The plan names every sample that will be submitted (review cp22-a R6-F1).
    keys = svc.ai_accept_keys(project_id, p.task_kind, p.scan_gen, p.subject, p.result_keys)
    chosen = {x["result_key"]: x for x in shown["all_samples"]}
    if not set(keys) <= set(chosen):
        raise PrometheosError("validation_error", "those samples are not available", {})
    shown["samples"] = [chosen[k] for k in keys]
    plan = {
        "op": "ai_accept",
        "task_kind": shown["task_kind"],
        "scan_gen": p.scan_gen,
        "subject": p.subject,
        "input_sha256": shown["input_sha256"],
        "samples": [{k: s[k] for k in ("result_key", "result", "model", "executor")}
                    for s in shown["samples"]],
    }  # fmt: skip
    text = (
        f"accept the intelligence layer's {shown['task_kind']} proposal for {p.subject} as "
        "proposed evidence (it never decides a family)"
    )
    return Built(plan, [text])


def _ai_accept_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                       grant: capabilities.HeldGrant, caller: reads.Caller,
                       options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    shown = svc.ai_accept_preview(project_id, plan["task_kind"], plan["scan_gen"], plan["subject"])
    if shown["input_sha256"] != plan["input_sha256"]:
        raise _changed("the file")
    keys = [x["result_key"] for x in plan["samples"]]
    if not set(keys) <= set(shown["available"]):
        raise _changed("the proposal's samples")  # review cp22-a R2-F2
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.ai_accept(
        project_id,
        plan["task_kind"],
        plan["scan_gen"],
        plan["subject"],
        authority=act,
        result_keys=keys,
    )
    return result


def _ai_run_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                  p: AiRunParams) -> Built:  # fmt: skip
    shown = svc.ai_run_per_call_preview(project_id, p.task_kind, p.scan_gen, p.paths)
    plan = {"op": "ai_run", **shown}
    lines = [
        f"run {shown['task_kind']} once, outside the standing policy, on "
        f"{len(shown['inputs'])} file(s) through {shown['model']} at {shown['endpoint_host']}",
        f"at most {shown['calls_at_most']} call(s), at most {shown['estimate_upper_usd']} USD "
        "(every input character counted as a token)",
        shown["data_policy"],
    ]
    if shown["denied"]:
        lines.append(f"not sent (restricted or private): {', '.join(shown['denied'])}")
    return Built(plan, lines)


def _ai_run_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                    grant: capabilities.HeldGrant, caller: reads.Caller,
                    options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    now = svc.ai_run_per_call_preview(project_id, plan["task_kind"], plan["scan_gen"],
                                      plan["paths"])  # fmt: skip
    for key in ("model", "provider_revision", "inputs", "denied", "estimate_upper_usd"):
        if now[key] != plan[key]:
            raise _changed("the run's files or the provider configuration", field=key)
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.ai_run(
        project_id, caller, task_kind=plan["task_kind"], scan_gen=plan["scan_gen"],
        paths=plan["paths"], per_call=plan, authority=act,
    )  # fmt: skip
    return result


# ----- home-level owner actions (TeX engine, speech models, skills, recording) -------------------


def _spend_then(
    store: ProjectStore, row: ApprovalRequest, grant: capabilities.HeldGrant
) -> OwnerAct:
    """Spend the server-held grant (committing the request's ``used`` state) and only then
    produce the owner act it stands for. No other code path builds an approval-grant act."""
    capability_id = approvals.spend(store, grant, "exact_request", _plan_digest(row.plan))
    return OwnerAct(
        channel="approval-grant",
        actor=f"{row.decided_by or 'owner'} via {row.id} ({capability_id[:8]})"[:100],
    )


def _tex_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
               p: DigestParams) -> Built:  # fmt: skip
    current = svc.tex_engine_plan(redact=True)
    if current["plan_digest"] != p.digest:
        raise _changed("the engine install plan", plan_digest=current["plan_digest"])
    keep = ("engine", "version", "release_tag", "download_url", "download_bytes", "sha256",
            "license", "license_url", "destination", "network")  # fmt: skip
    plan = {"op": "tex_engine_install", "plan_digest": p.digest} | {
        k: current[k] for k in keep if k in current
    }
    return Built(
        plan,
        [f"download and install Tectonic {current.get('version')} ({current.get('license')}, "
         f"{current.get('download_bytes')} bytes) into the PrometheOS home"],
    )  # fmt: skip


def _tex_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                 grant: capabilities.HeldGrant, caller: reads.Caller,
                 options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    if svc.tex_engine_plan()["plan_digest"] != row.plan["plan_digest"]:
        raise _changed("the engine install plan")
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.tex_engine_install(
        row.plan["plan_digest"], redact=True, authority=act, project_id=project_id,
        caller=caller, approval_request_id=row.id,
    )  # fmt: skip
    return result


def _model_install_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                         p: ModelInstallParams) -> Built:  # fmt: skip
    current = svc.audio_models_plan(p.model, redact_paths=True)
    if current["plan_sha256"] != p.plan_sha256:
        raise _changed("the model plan", plan_sha256=current["plan_sha256"])
    keep = ("model", "title", "engine", "repository", "revision", "license", "total_bytes",
            "total_mib", "location", "hosts_contacted", "transmits")  # fmt: skip
    plan = {"op": "audio_models_install", "plan_sha256": p.plan_sha256} | {
        k: current[k] for k in keep if k in current
    }
    return Built(
        plan,
        [f"download the speech model {p.model} ({current.get('total_mib')} MiB, "
         f"{current.get('license')}) into the PrometheOS home"],
    )  # fmt: skip


def _model_install_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                           grant: capabilities.HeldGrant, caller: reads.Caller,
                           options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    if svc.audio_models_plan(plan["model"])["plan_sha256"] != plan["plan_sha256"]:
        raise _changed("the model plan")
    act = _spend_then(store, row, grant)
    from prometheos.audio import models

    done = svc.audio_models_install(
        plan["model"], plan["plan_sha256"], None, authority=act, project_id=project_id,
        caller=caller, approval_request_id=row.id,
    )  # fmt: skip
    # Decision 0013 I4: agent transports see <PrometheOS models>/..., never the home path.
    result: dict[str, Any] = models.redact_location(svc.home, done)
    return result


def _installed_digest(svc: Any, model: str) -> str:
    status = svc.audio_models_status(redact_paths=True)
    entry = next((m for m in status["models"] if m.get("model") == model), None)
    return canonical_sha256(entry)


def _model_remove_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                        p: ModelRemoveParams) -> Built:  # fmt: skip
    plan = {
        "op": "audio_models_remove",
        "model": p.model,
        "installed_sha256": _installed_digest(svc, p.model),
    }
    return Built(plan, [f"delete the installed speech model {p.model}"])


def _model_remove_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                          grant: capabilities.HeldGrant, caller: reads.Caller,
                          options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    if _installed_digest(svc, plan["model"]) != plan["installed_sha256"]:
        raise _changed("the installed model")
    act = _spend_then(store, row, grant)
    from prometheos.audio import models

    done = svc.audio_models_remove(plan["model"], authority=act)
    result: dict[str, Any] = models.redact_location(svc.home, done)
    return result


def _skill_state(svc: Any, ref: str) -> dict[str, Any]:
    entry = svc.catalog.get(ref)
    return {
        "package": f"{entry['name']}@{entry['version']}",
        "package_digest": entry["package_digest"],
        "status": entry["status"],
    }


def _skill_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                 p: SkillTrustParams) -> Built:  # fmt: skip
    state = _skill_state(svc, p.ref)
    report = svc.skill_verify(p.ref)
    plan = {
        "op": "skill_trust",
        "ref": p.ref,
        **state,
        "unknown_rights": report.get("unknown_rights", []),
    }
    return Built(
        plan,
        [f"restore trust in the skill package {state['package']} (now {state['status']}); "
         "inspection cannot certify safety"],
    )  # fmt: skip


def _skill_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                   grant: capabilities.HeldGrant, caller: reads.Caller,
                   options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    plan: dict[str, Any] = row.plan
    state = _skill_state(svc, plan["ref"])
    if any(state[k] != plan[k] for k in state):
        raise _changed("the skill package")
    act = _spend_then(store, row, grant)
    result: dict[str, Any] = svc.skill_trust(plan["ref"], authority=act)
    return result


# ----- CP10.5: a whole-store backup (owner full-data scope; decision 0021) ------------------------


def _backup_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                  p: BackupParams) -> Built:  # fmt: skip
    plan = {"op": "backup_create", "project_id": project_id, "destination": "store exports"}
    return Built(
        plan,
        ["create a verified backup of the whole project store (every record, restricted and "
         "private ones included) in the store's exports folder"],
    )  # fmt: skip


def _backup_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                    grant: capabilities.HeldGrant, caller: reads.Caller,
                    options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    act = _spend_then(store, row, grant)
    owner = reads.Caller(actor=act.actor, scope="owner", full_data=True)
    made = svc.backup(project_id, None, owner)
    from prometheos.app.maintenance import Maintenance

    result: dict[str, Any] = Maintenance(svc).created_view(made)
    return result


def _projection_build(operation: str) -> Any:
    def build(
        svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller, p: Any
    ) -> Built:
        from pathlib import Path

        from prometheos import packages
        from prometheos.sources.paths import resolve_in_root

        root = svc.research_root(project_id, p.root_alias)
        target = Path(resolve_in_root(root, p.folder).path) if p.folder else root
        if operation == "skill_remove":
            projection = packages.remove_plan(target, p.name, p.host)
            lock = packages.load_lock(target.resolve())
            installed = next(
                i for i in lock["installs"] if i["name"] == p.name and i["host"] == p.host
            )
            entry = svc.catalog.find_digest(installed["package_digest"])
            if entry is None:
                raise PrometheosError(
                    "not_found", "installed package is absent from the catalogue", {}
                )
            ref = f"{entry['name']}@{entry['version']}"
        else:
            projection = packages.install_plan(svc.catalog, p.ref, p.host, target, p.resolve)
            ref = projection["package"]
        if projection["plan_sha256"] != p.plan_sha256:
            raise _changed("the skill projection plan")
        entry = svc.catalog.get(ref)
        report = packages.inspect(svc.catalog.entries(entry), svc.catalog)
        plan = {
            "op": operation,
            "params": p.model_dump(mode="json"),
            **_skill_state(svc, ref),
            "projection": projection,
            "files": report["files"],
            "findings": report["findings"],
            "unknown_rights": svc.skill_verify(ref)["unknown_rights"],
        }
        return Built(
            plan,
            [
                f"{operation}: {ref} into root {p.root_alias}/{p.folder or ''}; "
                "review every file, executable flag, finding and right"
            ],
        )

    return build


def _projection_execute(
    svc: Any,
    project_id: str,
    store: ProjectStore,
    row: ApprovalRequest,
    grant: capabilities.HeldGrant,
    caller: reads.Caller,
    options: dict[str, Any],
) -> dict[str, Any]:
    from prometheos.agentops import Ctx
    from prometheos.agentops.skills import _folder, _under

    kind = KINDS[row.operation]
    p: Any = parse_params(kind, row.plan["params"])
    current = kind.build(svc, project_id, store, caller, p)
    if canonical_sha256(current.plan) != row.plan_sha256:
        raise _changed("the skill plan, package digest or catalogue status")
    target, relative = _folder(Ctx(svc, project_id, caller), p.root_alias, p.folder)
    act = _spend_then(store, row, grant)
    record = project_id if p.record_in_project else None
    if row.operation == "skill_remove":
        result = svc.skill_remove(p.name, p.host, target, p.plan_sha256, record, authority=act)
    else:
        result = svc.skill_install(
            p.ref, p.host, target, p.plan_sha256, p.resolve, record, authority=act
        )
    value: dict[str, Any] = _under(target, result) | {
        "target": {"root_alias": p.root_alias, "path": relative}
    }
    return value


def _recording_build(svc: Any, project_id: str, store: ProjectStore, caller: reads.Caller,
                     p: RecordingParams) -> Built:  # fmt: skip
    plan = {
        "op": "recording_start",
        "title": p.title,
        "device_ids": list(p.device_ids),
        "sensitivity": p.sensitivity,
        "segment_seconds": p.segment_seconds,
        "max_seconds": p.max_seconds,
        "start_within_seconds": START_WINDOW_SECONDS,
        "capture": "the owner starts it in the hub (visible, with a stop control) or an "
        "interactive terminal; never inside an agent's server process",
        "consent": "granting this request is the owner's acknowledgement, at this computer, "
        "that every participant knows and agrees",
    }
    return Built(
        plan,
        [f"start a visible recording '{p.title}' from {', '.join(p.device_ids)} "
         f"({p.sensitivity}) for at most {p.max_seconds} s, within "
         f"{START_WINDOW_SECONDS // 60} minutes of the grant; the owner's grant is the consent "
         "acknowledgement"],
    )  # fmt: skip


def _recording_execute(svc: Any, project_id: str, store: ProjectStore, row: ApprovalRequest,
                       grant: capabilities.HeldGrant, caller: reads.Caller,
                       options: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from prometheos.audio import recorder

    plan: dict[str, Any] = row.plan
    owner = options.get("capture_owner")
    if not isinstance(owner, OwnerAct) or owner.channel not in CAPTURE_HOSTS:
        raise PrometheosError(
            "policy_denied",
            "a granted recording starts where the owner sees it: the owner starts it in the hub "
            "(Approvals) or an interactive terminal; capture never runs in an agent's server "
            "process",
            {"approval_request_id": row.id, "state": row.state},
        )
    decided = datetime.strptime(str(row.decided_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    window = int(plan.get("start_within_seconds", START_WINDOW_SECONDS))
    if approvals._now() > decided + timedelta(seconds=window):
        raise PrometheosError(
            "policy_denied",
            f"the start window of this recording grant ({window // 60} minutes) has passed; "
            "request it again",
            {"approval_request_id": row.id, "state": "start_window_passed"},
        )
    act = _spend_then(store, row, grant)
    request = recorder.SessionRequest(
        plan["title"], tuple(plan["device_ids"]), plan["sensitivity"], plan["segment_seconds"],
        True,
    )  # fmt: skip
    session = svc.recording_start(
        project_id, caller, request, options.get("backend"), authority=act
    )
    result: dict[str, Any] = {
        **session,
        "max_seconds": plan["max_seconds"],
        "approval_request_id": row.id,
    }
    return result


KINDS: dict[str, Kind] = {
    k.operation: k
    for k in (
        Kind("apply", "exact_request", None, _no_build, _plan_digest, _apply_execute),
        Kind(
            "ai_policy_grant",
            "exact_request",
            AiPolicyGrantParams,
            _ai_policy_build,
            _plan_digest,
            _ai_policy_execute,
        ),
        Kind(
            "ai_run",
            "exact_request",
            AiRunParams,
            _ai_run_build,
            _plan_digest,
            _ai_run_execute,
        ),
        Kind(
            "ai_accept",
            "exact_request",
            AiAcceptParams,
            _ai_accept_build,
            _plan_digest,
            _ai_accept_execute,
        ),
        Kind(
            "root_sensitivity_set",
            "exact_request",
            RootSensitivityParams,
            _root_sensitivity_build,
            _plan_digest,
            _root_sensitivity_execute,
        ),
        Kind(
            "job_resolve", "exact_request", JobResolveParams, _job_build, _job_digest, _job_execute
        ),
        Kind(
            "overleaf_push",
            "overleaf_push",
            PushParams,
            _push_build,
            lambda plan: str(plan["digest"]),
            _push_execute,
        ),
        Kind(
            "retention_purge_commit",
            "purge",
            PurgeParams,
            _purge_build,
            lambda plan: str(plan["purge_plan_sha256"]),
            _purge_execute,
        ),
        Kind(
            "provider_run",
            "provider_run",
            ProviderRunParams,
            _run_build,
            lambda plan: str(plan["approved_digest"]),
            _run_execute,
        ),
        Kind(
            "provider_configure",
            "provider_configure",
            ProviderConfigureParams,
            _configure_build,
            lambda plan: str(plan["digest"]),
            _configure_execute,
        ),
        Kind(
            "tex_engine_install",
            "exact_request",
            DigestParams,
            _tex_build,
            _plan_digest,
            _tex_execute,
        ),
        Kind(
            "audio_models_install",
            "exact_request",
            ModelInstallParams,
            _model_install_build,
            _plan_digest,
            _model_install_execute,
        ),
        Kind(
            "audio_models_remove",
            "exact_request",
            ModelRemoveParams,
            _model_remove_build,
            _plan_digest,
            _model_remove_execute,
        ),
        Kind(
            "skill_trust",
            "exact_request",
            SkillTrustParams,
            _skill_build,
            _plan_digest,
            _skill_execute,
        ),
        Kind(
            "skill_install",
            "exact_request",
            SkillProjectionParams,
            _projection_build("skill_install"),
            _plan_digest,
            _projection_execute,
        ),
        Kind(
            "skill_update",
            "exact_request",
            SkillProjectionParams,
            _projection_build("skill_update"),
            _plan_digest,
            _projection_execute,
        ),
        Kind(
            "skill_remove",
            "exact_request",
            SkillRemovalParams,
            _projection_build("skill_remove"),
            _plan_digest,
            _projection_execute,
        ),
        Kind(
            "recording_start",
            "exact_request",
            RecordingParams,
            _recording_build,
            _plan_digest,
            _recording_execute,
        ),
        Kind(
            "backup_create",
            "exact_request",
            BackupParams,
            _backup_build,
            _plan_digest,
            _backup_execute,
        ),
        Kind(
            "event_acknowledge",
            "exact_request",
            EventAcknowledgeParams,
            _ack_build,
            _plan_digest,
            _ack_execute,
        ),
        Kind(
            "library_dedup_resolve",
            "exact_request",
            LibraryResolveParams,
            _resolve_build,
            _plan_digest,
            _resolve_execute,
        ),
        Kind(
            "library_dedup_undo",
            "exact_request",
            LibraryUndoParams,
            _undo_build,
            _plan_digest,
            _undo_execute,
        ),
        Kind(
            "project_root_add",
            "exact_request",
            ProjectRootAddParams,
            _root_build,
            _plan_digest,
            _root_execute,
        ),
        Kind(
            "zotero_import_commit",
            "exact_request",
            ZoteroCommitParams,
            _zotero_build,
            _plan_digest,
            _zotero_execute,
        ),
        Kind(
            "adoption_decide",
            "exact_request",
            AdoptionDecideParams,
            _decide_build,
            _plan_digest,
            _decide_execute,
        ),
        Kind(
            "adoption_classify",
            "exact_request",
            AdoptionClassifyParams,
            _classify_build,
            _plan_digest,
            _classify_execute,
        ),
        Kind(
            "adoption_outline_confirm",
            "exact_request",
            AdoptionOutlineParams,
            _outline_build,
            _plan_digest,
            _outline_execute,
        ),
        Kind(
            "adoption_export_markdown",
            "exact_request",
            AdoptionExportParams,
            _export_build,
            _plan_digest,
            _export_execute,
        ),
        Kind(
            "adoption_baseline_accept",
            "exact_request",
            AdoptionBaselineParams,
            _baseline_build,
            _plan_digest,
            _baseline_execute,
        ),
    )
}
"""Registry operation id -> how its approval request is planned and resumed."""


def _projection_review(svc: Any, project_id: str, row: ApprovalRequest) -> dict[str, Any]:
    kind = KINDS[row.operation]
    params = parse_params(kind, row.plan["params"])
    current = kind.build(
        svc, project_id, svc.open(project_id), caller_for(row, "owner-review"), params
    )
    return {
        "matches_plan": canonical_sha256(current.plan) == row.plan_sha256,
        "owner_view": current.plan,
    }


OWNER_REVIEWS: dict[str, Callable[[Any, str, ApprovalRequest], dict[str, Any]]] = {
    "skill_install": _projection_review,
    "skill_update": _projection_review,
    "skill_remove": _projection_review,
    "overleaf_push": _push_review,
    "retention_purge_commit": _purge_review,
    "project_root_add": _root_review,
}
"""Kinds whose full effect is visible only to the owner: computed on demand for one request,
never in a listing (F2, F12)."""


def parse_params(kind: Kind, params: Any) -> StrictModel:
    if kind.params is None:
        raise PrometheosError("validation_error", f"{kind.operation} takes no parameters here", {})
    try:
        raw = params if isinstance(params, bytes) else dumps_canonical(params)
        return parse_model(kind.params, raw)
    except ValidationError as exc:
        errors = [
            {"loc": "/".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]
        ]
        raise PrometheosError(
            "validation_error", f"invalid parameters for {kind.operation}", {"errors": errors}
        ) from exc
    except (CanonicalJSONError, ValueError) as exc:
        raise PrometheosError(
            "validation_error", f"invalid parameters for {kind.operation}", {}
        ) from exc
