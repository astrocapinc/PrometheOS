"""The intelligence layer's operations (A07, CP22.4; spec/04; decision 0094).

The service's boundary to ``prometheos.intelligence``:

- **The standing policy.** ``policy_show`` reads it; ``policy_preview`` shows what a grant binds
  (the policy's digest and the provider configuration revision); ``policy_grant`` stores it only
  with an owner act, voids the one before, and sets the home's monthly cap; ``policy_revoke``
  stops new calls at once and is the owner's alone (an agent's attempt answers
  ``policy_denied``; an agent's grant becomes an approval request).
- **Root sensitivity.** Each research root reads as restricted until the owner sets it; lowering
  it is an owner decision bound to the alias, its resolved folder and the value; raising it is
  not protected; pointing the alias at another folder makes it restricted again. A file's
  sensitivity is the highest of its root's and of any record citing its exact bytes.
- **Runs.** ``run`` takes adoption files (root alias and path) for a registered task, builds each
  bundle from digest-pinned bytes, and runs it inside the policy on the gateway, or on recorded
  cassettes; ``dry_run`` shows the calls and the upper cost estimate and sends nothing.
  Restricted input never leaves (``policy_denied``); a run outside the policy is the CP6.3
  per-call route (``approval_required``); an agent runs only where the policy lets agents.
- **Reads.** ``results`` lists stored samples; ``evaluations`` lists measurements and trust.

Nothing here sets a status, a class, a baseline, a relation, a default or a permission (D16).
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from prometheos.app.authority import require_owner
from prometheos.domain.canonical import canonical_sha256
from prometheos.domain.models import PrometheosError, utc_now
from prometheos.intelligence import policy as ai_policy
from prometheos.intelligence import results, spend, tasks
from prometheos.intelligence.gateway import Gateway, estimate_cost
from prometheos.intelligence.run import CassetteExecutor, GatewayExecutor, Subject, run
from prometheos.providers import config as provider_config
from prometheos.providers import secrets as provider_secrets
from prometheos.store import db
from prometheos.store.project import ProjectStore

SENSITIVITIES = ("normal", "restricted", "private")


# ----- the standing policy --------------------------------------------------------------------


def _in_force(store: ProjectStore) -> dict[str, Any] | None:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT revision, policy, policy_sha256, provider_revision, granted_by, granted_at "
            "FROM ai_policy WHERE state = 'in_force'"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"revision": row[0], "policy": json.loads(row[1]), "policy_sha256": row[2],
            "provider_revision": row[3], "granted_by": row[4], "granted_at": row[5]}  # fmt: skip


def policy_show(svc: Any, project_id: str) -> dict[str, Any]:
    store = svc.open(project_id)
    conn = store.connect()
    try:
        config, revision = provider_config.load(conn)
    finally:
        conn.close()
    granted = _in_force(store)
    void = granted is not None and (
        granted["provider_revision"] != revision or not ai_policy.openrouter_configured(config)
    )
    return {
        "policy": granted,
        "in_force": granted is not None and not void,
        "void_reason": "the provider configuration changed since the grant" if void else None,
        "provider_revision": revision,
        "provider_is_openrouter": ai_policy.openrouter_configured(config),
        "key": provider_secrets.health(svc._secrets(), config.credential_ref),
        "spend": spend.summary(svc.home),
        "tasks": sorted(tasks.REGISTRY),
    }


def policy_base(svc: Any, project_id: str) -> dict[str, Any]:
    """The state a grant request is bound to: the latest policy revision and its state, so a
    grant, change or revocation since the request voids it (review cp22-a F6)."""
    store = svc.open(project_id)
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT revision, state FROM ai_policy ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {"revision": row[0], "state": row[1]} if row else {"revision": 0, "state": "none"}


def policy_preview(svc: Any, project_id: str, document: Any) -> dict[str, Any]:
    try:
        parsed = ai_policy.StandingPolicy.model_validate(document, strict=False)
    except ValueError as exc:
        raise PrometheosError(
            "validation_error", "the standing policy is invalid", {"errors": str(exc)[:1500]}
        ) from exc
    unknown = [t.task_kind for t in parsed.tasks if t.task_kind not in tasks.REGISTRY]
    if unknown:
        raise PrometheosError("validation_error", "unknown task kinds", {"task_kinds": unknown})
    for t in parsed.tasks:
        _distinct_models(tasks.get(t.task_kind), t)
    store = svc.open(project_id)
    conn = store.connect()
    try:
        config, revision = provider_config.load(conn)
    finally:
        conn.close()
    if not ai_policy.openrouter_configured(config):
        raise PrometheosError(
            "validation_error",
            "configure the provider first: remote-allowed, the OpenRouter endpoint "
            "https://openrouter.ai/api/v1 and a credential reference",
            {},
        )
    current = _in_force(store)
    return {
        "policy": parsed.model_dump(mode="json"),
        "policy_sha256": ai_policy.policy_sha256(parsed),
        "provider_revision": revision,
        "binding_sha256": ai_policy.binding(parsed, revision),
        "replaces": current["revision"] if current else None,
        "summary": [
            f"{t.task_kind}: " + ", ".join(f"{m.model} via {m.provider}" for m in t.models)
            for t in parsed.tasks
        ]
        + [
            f"caps: ${parsed.run_cap_usd} per run, ${parsed.monthly_cap_usd} a month (home)",
            "agents may start runs" if parsed.agents_may_run else "agents may not start runs",
            "only normal-sensitivity input; zero data retention; data collection denied",
        ],
    }


def policy_grant(
    svc: Any, project_id: str, document: Any, binding_sha256: str, authority: object
) -> dict[str, Any]:
    act = require_owner(authority, "granting the standing policy")
    shown = policy_preview(svc, project_id, document)
    if shown["binding_sha256"] != binding_sha256:
        raise PrometheosError(
            "revision_conflict",
            "the policy or the provider configuration changed since it was reviewed",
            {"binding_sha256": shown["binding_sha256"]},
        )
    store = svc.open(project_id)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            now = utc_now()
            conn.execute(
                "UPDATE ai_policy SET state = 'superseded', ended_by = ?, ended_at = ? "
                "WHERE state = 'in_force'",
                (act.actor, now),
            )
            cursor = conn.execute(
                "INSERT INTO ai_policy (policy, policy_sha256, provider_revision, state, "
                "granted_by, granted_at) VALUES (?, ?, ?, 'in_force', ?, ?)",
                (json.dumps(shown["policy"], sort_keys=True), shown["policy_sha256"],
                 shown["provider_revision"], act.actor, now),
            )  # fmt: skip
            revision = cursor.lastrowid
    finally:
        conn.close()
    spend.set_monthly_cap(svc.home, Decimal(shown["policy"]["monthly_cap_usd"]))
    return {"revision": revision, "policy_sha256": shown["policy_sha256"],
            "provider_revision": shown["provider_revision"], "in_force": True}  # fmt: skip


def policy_revoke(svc: Any, project_id: str, authority: object) -> dict[str, Any]:
    act = require_owner(authority, "revoking the standing policy")
    store = svc.open(project_id)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            changed = conn.execute(
                "UPDATE ai_policy SET state = 'revoked', ended_by = ?, ended_at = ? "
                "WHERE state = 'in_force'",
                (act.actor, utc_now()),
            ).rowcount
    finally:
        conn.close()
    return {"revoked": bool(changed), "in_force": False}


# ----- root sensitivity ---------------------------------------------------------------------------


def _root_folder(svc: Any, project_id: str, alias: str) -> str:
    return str(Path(svc.research_root(project_id, alias)).resolve())


def root_sensitivity_show(svc: Any, project_id: str) -> dict[str, Any]:
    from prometheos.app import sources

    store = svc.open(project_id)
    conn = store.connect()
    try:
        settings, _ = sources._settings(conn, project_id)
        rows = {r[0]: r for r in conn.execute(
            "SELECT root_alias, folder, sensitivity, set_by, set_at FROM root_sensitivity"
        ).fetchall()}  # fmt: skip
    finally:
        conn.close()
    out = []
    for root in settings.allowed_roots:
        folder = str(Path(root.path).resolve())
        row = rows.get(root.alias)
        bound = row is not None and row[1] == folder
        out.append({
            "root_alias": root.alias,
            "sensitivity": row[2] if bound and row is not None else "restricted",
            "set_by_owner": bound,
            "note": None if bound else (
                "restricted until the owner sets it" if row is None
                else "the alias now points at another folder: restricted again"),
        })  # fmt: skip
    return {"roots": out}


def root_sensitivity_set(
    svc: Any, project_id: str, alias: str, sensitivity: str, authority: object | None
) -> dict[str, Any]:
    if sensitivity not in SENSITIVITIES:
        raise PrometheosError(
            "validation_error", "sensitivity is normal, restricted or private", {}
        )
    folder = _root_folder(svc, project_id, alias)
    current = next(
        r for r in root_sensitivity_show(svc, project_id)["roots"] if r["root_alias"] == alias
    )
    lowering = SENSITIVITIES.index(sensitivity) < SENSITIVITIES.index(current["sensitivity"])
    actor = "agent-or-cli"
    if lowering:
        actor = require_owner(authority, "lowering a root's sensitivity").actor
    store = svc.open(project_id)
    conn = store.connect()
    try:
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO root_sensitivity (root_alias, folder, sensitivity, set_by, set_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(root_alias) DO UPDATE SET folder = "
                "excluded.folder, sensitivity = excluded.sensitivity, set_by = excluded.set_by, "
                "set_at = excluded.set_at",
                (alias, folder, sensitivity, actor, utc_now()),
            )
    finally:
        conn.close()
    return {"root_alias": alias, "sensitivity": sensitivity, "lowered": lowering}


def root_levels(svc: Any, project_id: str) -> list[tuple[str, Path, str]]:
    """(alias, resolved folder, effective sensitivity) of every approved root."""
    from prometheos.app import sources

    store = svc.open(project_id)
    conn = store.connect()
    try:
        settings, _ = sources._settings(conn, project_id)
    finally:
        conn.close()
    shown = {
        r["root_alias"]: r["sensitivity"] for r in root_sensitivity_show(svc, project_id)["roots"]
    }
    return [(r.alias, Path(r.path).resolve(), shown.get(r.alias, "restricted"))
            for r in settings.allowed_roots]  # fmt: skip


def file_sensitivity(
    conn: sqlite3.Connection,
    roots: list[tuple[str, Path, str]],
    absolute: Path,
    sha256: str | None,
) -> str:
    """The highest of (A07.4, spec/03 (v); review cp22-a F1-F3):
    - every approved root that contains the file (roots may nest or repeat);
    - the version records of the file: those bound to it by root alias and path (whatever bytes
      it holds now) and those with its exact bytes, and their sources;
    - every record citing one of those, through its body (``refs``: evidence lists, links) or a
      relation."""
    target = absolute.resolve()
    levels: list[str] = []
    bindings: list[tuple[str, str]] = []
    for alias, folder, level in roots:
        if target == folder or folder in target.parents:
            levels.append(level)
            bindings.append((alias, target.relative_to(folder).as_posix()))
    if not levels:
        levels.append("restricted")
    found: set[str] = set()
    for alias, relative in bindings:
        sources_bound = conn.execute(
            "SELECT r.id FROM records r JOIN revisions rv ON rv.record_id = r.id "
            "AND rv.rev = r.current_rev JOIN bodies b ON b.id = rv.body_id WHERE r.kind = 'source' "
            "AND json_extract(b.encoded, '$.root_binding.alias') = ? "
            "AND json_extract(b.encoded, '$.root_binding.path') = ?",
            (alias, relative),
        ).fetchall()
        found.update(r[0] for r in sources_bound)
    if sha256:
        found.update(r[0] for r in conn.execute(
            "SELECT r.id FROM records r JOIN revisions rv ON rv.record_id = r.id "
            "AND rv.rev = r.current_rev JOIN bodies b ON b.id = rv.body_id "
            "WHERE r.kind = 'version' "
            "AND json_extract(b.encoded, '$.sha256') = ?",
            (sha256,),
        ).fetchall())  # fmt: skip
    if found:
        marks = ",".join("?" * len(found))
        ids = list(found)
        # the versions of every source found, and the source of every version found
        found.update(r[0] for r in conn.execute(
            f"SELECT r.id FROM records r JOIN revisions rv ON rv.record_id = r.id "  # noqa: S608
            f"AND rv.rev = r.current_rev JOIN bodies b ON b.id = rv.body_id "
            f"WHERE r.kind = 'version' "
            f"AND json_extract(b.encoded, '$.source_id') IN ({marks})", ids,
        ).fetchall())  # fmt: skip
        found.update(r[0] for r in conn.execute(
            f"SELECT json_extract(b.encoded, '$.source_id') FROM records r JOIN revisions rv "  # noqa: S608
            f"ON rv.record_id = r.id AND rv.rev = r.current_rev JOIN bodies b ON b.id = rv.body_id "
            f"WHERE r.id IN ({marks}) AND r.kind = 'version'", ids,
        ).fetchall() if r[0])  # fmt: skip
        marks = ",".join("?" * len(found))
        ids = list(found)
        citing = {r[0] for r in conn.execute(
            f"SELECT from_id FROM refs WHERE to_id IN ({marks}) UNION "  # noqa: S608
            f"SELECT source_id FROM relations WHERE retired_seq IS NULL AND target_id IN ({marks})",
            ids + ids,
        ).fetchall()}  # fmt: skip
        everything = list(found | citing)
        marks = ",".join("?" * len(everything))
        levels += [r[0] for r in conn.execute(
            f"SELECT sensitivity FROM records WHERE id IN ({marks})", everything  # noqa: S608
        ).fetchall()]  # fmt: skip
    return max(levels, key=SENSITIVITIES.index)


# ----- runs and reads ---------------------------------------------------------------------------


def _subjects(svc: Any, project_id: str, store: ProjectStore, task: tasks.Task, scan_gen: int,
              paths: list[str]) -> tuple[list[Subject], list[dict[str, Any]]]:  # fmt: skip
    """Bundles for the named files; a file that cannot be bundled is reported, never sent.
    The roots are read before the store connection opens (one connection to a live store at a
    time in a process). A bundle's sensitivity is the highest of its file's and of every file it
    names (the candidates of a status-statement bundle)."""
    import hashlib

    from prometheos.adoption import assist, tables
    from prometheos.adoption.contracts import AdoptionFile, disk_path

    if task.assist_kind is None:
        raise PrometheosError("unsupported", f"{task.kind} has no adoption subjects", {})
    conn = store.connect()
    try:
        alias = tables.require_scan(conn, scan_gen).root_alias
    finally:
        conn.close()
    root = Path(svc.research_root(project_id, alias))
    roots = root_levels(svc, project_id)

    def family_reread(members: list[str]) -> Any:
        def digest() -> str | None:
            from prometheos.domain.canonical import canonical_sha256

            found = []
            for member in members:
                target = root / disk_path(member)
                try:
                    data = target.read_bytes() if target.is_file() else None
                except OSError:
                    data = None
                found.append(
                    (member, hashlib.sha256(data).hexdigest() if data is not None else None)
                )
            return canonical_sha256(sorted(found))

        return digest

    def reread(row: Any) -> Any:
        # The inventory's own guarded read (no links, no placeholders, archive members, escaped
        # names; review cp22-a R2-F3).
        def digest() -> str | None:
            data = assist.read_row_bytes(root, row)
            return None if data is None else hashlib.sha256(data).hexdigest()

        return digest

    subjects, skipped = [], []
    conn = store.connect()
    try:
        families = (
            {f.key: f for f in tables.list_families(conn, scan_gen)}
            if task.assist_kind == "propose_family_class"
            else {}
        )
        for path in paths:
            try:
                bundle = assist._bundle(conn, root, scan_gen, task.assist_kind, path)
            except PrometheosError as exc:
                skipped.append({"subject": path, "outcome": "not_run", "reasons": [exc.code]})
                continue
            if task.assist_kind == "propose_family_class":
                # A family subject (decision 0107): its sensitivity is the highest of its
                # members', and its digest is re-read from every member before a reply counts.
                members = sorted(m.path for m in families[path].members)
                sensitivity = max(
                    (file_sensitivity(conn, roots, root / disk_path(m), None) for m in members),
                    key=SENSITIVITIES.index,
                )
                subjects.append(Subject(bundle, bundle.subject_sha256, sensitivity,
                                        [f"{alias}:{m}" for m in members],
                                        family_reread(members), scan_gen))  # fmt: skip
                continue
            row = tables.from_row(AdoptionFile, conn.execute(
                "SELECT * FROM adoption_files WHERE scan_gen = ? AND path = ?", (scan_gen, path)
            ).fetchone())  # fmt: skip
            named = [path, *bundle.input.get("candidates", [])]
            sensitivity = max(
                (file_sensitivity(conn, roots, root / disk_path(p),
                                  bundle.subject_sha256 if p == path else None)
                 for p in named),
                key=SENSITIVITIES.index,
            )  # fmt: skip
            subjects.append(Subject(bundle, bundle.subject_sha256, sensitivity,
                                    [f"{alias}:{path}"], reread(row), scan_gen))  # fmt: skip
    finally:
        conn.close()
    return subjects, skipped


def _guard(svc: Any, project_id: str, revision: int, policy_sha256: str) -> Any:
    """Before every send: the same policy still in force on the same provider revision, or the
    batch stops (A07.4 "Revocation stops new calls at once"; review cp22-a F5)."""
    from prometheos.intelligence.gateway import BatchStop

    def check() -> None:
        store = svc.open(project_id)
        conn = store.connect()
        try:
            config, current = provider_config.load(conn)
        finally:
            conn.close()
        granted = _in_force(store)
        if (
            granted is None
            or granted["policy_sha256"] != policy_sha256
            or granted["provider_revision"] != current
            or current != revision
            or not ai_policy.openrouter_configured(config)
        ):
            raise BatchStop("policy_revoked", "the standing policy is no longer in force")

    return check


def _distinct_models(task: tasks.Task, grant: ai_policy.TaskGrant) -> None:
    """A task that needs ``k`` agreeing samples needs ``k`` different evaluated models: CP17.4
    counts agreement from one model as repeatability, not independence (review cp22-a R4-F2)."""
    if task.agreement >= 2 and len({m.model for m in grant.models}) < task.agreement:
        raise PrometheosError(
            "validation_error",
            f"{task.kind} needs {task.agreement} different models agreeing; list "
            f"{task.agreement} evaluated models for it",
            {"task_kind": task.kind, "needed": task.agreement},
        )


def _per_call_pair(config: provider_config.ProviderConfig) -> ai_policy.EvaluatedModel:
    """The provider configuration's model as the one pair of a per-call run (no provider pin,
    no listed parameters, the owner's prices)."""
    from datetime import UTC, datetime

    from prometheos.intelligence.gateway import PER_CALL_PROVIDER

    if config.price_input_per_mtok_usd is None or config.price_output_per_mtok_usd is None:
        raise PrometheosError(
            "validation_error",
            "a run outside the standing policy needs the configured model's prices",
            {},
        )
    return ai_policy.EvaluatedModel(
        model=str(config.model), provider=PER_CALL_PROVIDER, company="per-call",
        supported_parameters=[], lowest_effort=None,
        price_input_per_mtok_usd=config.price_input_per_mtok_usd,
        price_output_per_mtok_usd=config.price_output_per_mtok_usd,
        metadata_as_of=datetime.now(UTC).strftime("%Y-%m-%d"),
    )  # fmt: skip


def per_call_preview(
    svc: Any, project_id: str, *, task_kind: str, scan_gen: int, paths: list[str]
) -> dict[str, Any]:
    """The CP6.3 per-call preview of a run outside the standing policy: what would be sent where,
    in how many calls, at most at what cost (spec/04)."""
    task = tasks.get(task_kind)
    store = svc.open(project_id)
    conn = store.connect()
    try:
        config, revision = provider_config.load(conn)
    finally:
        conn.close()
    if not ai_policy.openrouter_configured(config):
        raise PrometheosError(
            "capability_unavailable",
            "the intelligence layer is off: configure the provider (remote-allowed, the "
            "OpenRouter endpoint, a credential reference) before a layer run. Meanwhile the "
            "rules' own result stands and your coding agent can do this reading through adopt "
            "assist (prepare, then submit)",
            {"capability": f"intelligence.{task.kind}"[:64], "blocker": "not_configured"},
        )
    if task.agreement >= 2:
        raise PrometheosError(
            "capability_unavailable",
            f"{task.kind} needs {task.agreement} different models agreeing and a run outside "
            "the standing policy has one: grant the task in a standing policy with "
            f"{task.agreement} evaluated models, or run it through your coding agent's path "
            "(adopt assist); the rules' own result stands meanwhile",
            {"capability": f"intelligence.{task.kind}"[:64], "blocker": "not_configured"},
        )
    pair = _per_call_pair(config)
    subjects, skipped = _subjects(svc, project_id, store, task, scan_gen, paths)
    normal = [x for x in subjects if x.sensitivity == "normal"]
    # The CP6.3 budget the owner configured bounds a per-call run (review cp22-a R3-F2).
    budget = config.budget
    calls = len(normal) * task.agreement
    if calls > budget.max_calls_per_run:
        raise PrometheosError(
            "budget_exceeded",
            f"a run outside the standing policy may make {budget.max_calls_per_run} call(s) "
            f"(the provider budget); this one needs {calls}: name fewer files or grant a policy",
            {"cap": "calls_per_run", "needed": calls, "limit": budget.max_calls_per_run},
        )
    for x in normal:
        system, user = task.messages(x.bundle.input)
        if len(system) + len(user) > budget.max_input_chars:
            raise PrometheosError(
                "budget_exceeded",
                f"{x.bundle.subject} is larger than the provider budget's input limit",
                {"cap": "input_chars", "limit": budget.max_input_chars},
            )
    upper = Decimal(0)
    for x in normal:
        system, user = task.messages(x.bundle.input)
        upper += (
            estimate_cost(pair, len(system) + len(user), task.max_output_tokens) * task.agreement
        )
    if budget.spend_cap_usd is not None:
        from prometheos.providers import ledger as provider_ledger

        conn = store.connect()
        try:
            spent = provider_ledger.totals(conn).spent_usd
        finally:
            conn.close()
        if spent + upper > Decimal(budget.spend_cap_usd):
            raise PrometheosError(
                "budget_exceeded",
                "this run could take the provider's spend cap past its limit; nothing was sent",
                {"cap": "provider_spend_cap", "limit": budget.spend_cap_usd},
            )
    return {
        "task_kind": task.kind,
        "scan_gen": scan_gen,
        "paths": paths,
        "max_output_tokens": min(task.max_output_tokens, budget.max_output_tokens),
        "routing": "OpenRouter chooses the serving provider (zero retention only); no provider "
        "is pinned outside the policy",
        "model": pair.model,
        "endpoint_host": "openrouter.ai",
        "provider_revision": revision,
        "inputs": {x.bundle.subject: x.bundle.input_sha256 for x in normal},
        "denied": [x.bundle.subject for x in subjects if x.sensitivity != "normal"],
        "skipped": [i["subject"] for i in skipped],
        "calls_at_most": len(normal) * task.agreement,
        "estimate_upper_usd": str(upper),
        "data_policy": "zero data retention; data collection denied; outputs are hints only",
    }


def _outside_policy(
    svc: Any, project_id: str, store: ProjectStore, task: tasks.Task, subjects: list[Subject],
    skipped: list[dict[str, Any]], config: provider_config.ProviderConfig, revision: int, *,
    scan_gen: int, paths: list[str], dry_run: bool, caller: Any,
    per_call: dict[str, Any] | None, authority: object,
) -> dict[str, Any]:  # fmt: skip
    """The CP6.3 per-call route for a run outside the standing policy (spec/04): the exact plan
    is stored as an approval request and nothing is sent until the owner grants it; the granted
    run is that one run, capped at its upper estimate, its outputs hints only."""
    if per_call is None:
        preview = per_call_preview(svc, project_id, task_kind=task.kind, scan_gen=scan_gen,
                                   paths=paths)  # fmt: skip
        if dry_run:
            return {"dry_run": True, "outside_policy": True, **preview}
        svc.approval_request(
            project_id, caller, "ai_run",
            {"task_kind": task.kind, "scan_gen": scan_gen, "paths": paths},
        )  # fmt: skip
    require_owner(authority, "a layer run outside the standing policy")
    assert per_call is not None
    pair = _per_call_pair(config)
    if pair.model != per_call["model"] or revision != per_call["provider_revision"]:
        raise PrometheosError(
            "revision_conflict", "the provider configuration changed since approval", {}
        )
    cap = Decimal(per_call["estimate_upper_usd"])
    grant = ai_policy.TaskGrant(
        task_kind=task.kind, models=[pair],
        single_company_reason="one run the owner approved per call, outside the policy",
        task_ceiling_usd=str(cap) if cap > 0 else "0.000001",
    )  # fmt: skip
    gateway = Gateway(config, svc._secrets(), svc._transport(), svc.home, project_id)

    def guard() -> None:  # the provider configuration the owner approved, unchanged
        conn = store.connect()
        try:
            _, current = provider_config.load(conn)
        finally:
            conn.close()
        if current != revision:
            from prometheos.intelligence.gateway import BatchStop

            raise BatchStop("policy_revoked", "the provider configuration changed")

    import dataclasses

    # The provider budget's output limit (review cp22-a R3-F2); the template is unchanged.
    bounded = dataclasses.replace(task, max_output_tokens=int(per_call["max_output_tokens"]))
    done = run(store, bounded, subjects, GatewayExecutor(store, gateway, grant, cap, guard))
    done["items"] += skipped
    done["outside_policy"] = True
    return done


def run_task(
    svc: Any,
    project_id: str,
    *,
    task_kind: str,
    scan_gen: int,
    paths: list[str],
    agent: bool,
    dry_run: bool = False,
    cassettes: Path | None = None,
    caller: Any = None,
    per_call: dict[str, Any] | None = None,
    authority: object = None,
) -> dict[str, Any]:
    task = tasks.get(task_kind)
    if not paths or len(paths) > 500:
        raise PrometheosError("validation_error", "name between 1 and 500 files", {})
    store = svc.open(project_id)
    conn = store.connect()
    try:
        config, revision = provider_config.load(conn)
    finally:
        conn.close()
    subjects, skipped = _subjects(svc, project_id, store, task, scan_gen, paths)
    if cassettes is not None:
        executor: Any = CassetteExecutor.from_folder(cassettes)
        grant = None
    else:
        worst = max((s.sensitivity for s in subjects), key=SENSITIVITIES.index, default="normal")
        # Restricted subjects are denied one by one by the run; the gate is asked as for normal.
        outside = False
        try:
            grant = ai_policy.check_run(
                _in_force(store), config, revision, task_kind=task.kind,
                sensitivity="normal" if worst != "normal" else worst, agent=agent,
            )  # fmt: skip
        except ai_policy.OutsidePolicy:
            outside = True
        if outside:
            return _outside_policy(
                svc, project_id, store, task, subjects, skipped, config, revision,
                scan_gen=scan_gen, paths=paths, dry_run=dry_run, caller=caller,
                per_call=per_call, authority=authority,
            )  # fmt: skip
        assert grant is not None
        _distinct_models(task, grant)
        granted = _in_force(store)
        assert granted is not None
        run_cap = Decimal(granted["policy"]["run_cap_usd"])
        gateway = Gateway(config, svc._secrets(), svc._transport(), svc.home, project_id)
        guard = _guard(svc, project_id, revision, granted["policy_sha256"])
        executor = GatewayExecutor(store, gateway, grant, run_cap, guard)
    if dry_run:
        normal = [x for x in subjects if x.sensitivity == "normal"]

        def upper_of(x: Subject, pairs: list[Any]) -> Decimal:
            system, user = task.messages(x.bundle.input)
            return sum(
                (estimate_cost(m, len(system) + len(user), task.max_output_tokens)
                 for m in pairs),
                Decimal(0),
            ) * task.agreement  # fmt: skip

        pairs = list(grant.models) if grant else []
        upper = sum((upper_of(x, pairs) for x in normal), Decimal(0))
        return {
            "dry_run": True,
            "task_kind": task.kind,
            "subjects": len(subjects),
            "denied": len(subjects) - len(normal),
            "skipped": skipped,
            "calls_at_most": len(normal) * task.agreement * (len(grant.models) if grant else 0),
            "estimate_upper_usd": str(upper),
            "estimate_label": "at most: every sample of every allowed file through every "
            "evaluated pair, each input character counted as a token; no project content is sent "
            "(the key's remaining limit is read)",
            "spend": spend.summary(svc.home),
            "key": executor.gateway.key_limit() if grant else None,
        }
    if grant is not None:
        _cap_by_key(executor)
    done = run(store, task, subjects, executor)
    done["items"] += skipped
    return done


def _cap_by_key(executor: GatewayExecutor) -> None:
    """The key's own remaining limit, read before the batch (A07.6; review cp22-a F10): a used-up
    key refuses the batch and the run cap never exceeds what the key has left. Shared by every
    gateway run (review map-ai-spread-a R1-F4)."""
    limit = executor.gateway.key_limit()
    remaining = None if limit is None else limit.get("limit_remaining")
    if isinstance(remaining, int | float) and not isinstance(remaining, bool):
        left = Decimal(str(remaining))
        if left <= 0:
            raise PrometheosError(
                "budget_exceeded", "the key's own limit is used up", {"cap": "key"}
            )
        executor.run_cap = min(executor.run_cap, left)


def accept_preview(
    svc: Any, project_id: str, *, task_kind: str, scan_gen: int, subject: str
) -> dict[str, Any]:
    """What accepting the layer's proposal for one file would record: the stored samples that
    passed every check for the file's current bytes, and their provenance."""
    from prometheos.adoption import assist, tables

    task = tasks.get(task_kind)
    if task.assist_kind is None:
        raise PrometheosError("unsupported", f"{task.kind} has no adoption subjects", {})
    store = svc.open(project_id)
    conn = store.connect()
    try:
        alias = tables.require_scan(conn, scan_gen).root_alias
    finally:
        conn.close()
    root = Path(svc.research_root(project_id, alias))
    conn = store.connect()
    try:
        bundle = assist._bundle(conn, root, scan_gen, task.assist_kind, subject)
        rows = conn.execute(
            "SELECT result_key, output, model, served_model, served_provider, executor, call_id, "
            "run_id, sample FROM ai_results WHERE task_kind = ? AND task_version = ? "
            "AND template_sha256 = ? AND wire_sha256 = ? AND input_sha256 = ? "
            "AND outcome = 'accepted' ORDER BY sample, created_at",
            (task.kind, task.version, task.template_sha256, task.wire_sha256,
             bundle.input_sha256),
        ).fetchall()  # fmt: skip
        taken = {
            str(r[0]) for r in conn.execute(
                "SELECT json_extract(response, '$.submitted_by') FROM adoption_assist "
                "WHERE cache_key LIKE ?", (bundle.assist_id + "#%",),
            ).fetchall() if r[0]
        }  # fmt: skip
    finally:
        conn.close()
    unused = [
        {"result_key": r[0], "result": json.loads(r[1])["result"], "model": r[3] or r[2],
         "provider": r[4], "executor": r[5], "call_id": r[6], "run_id": r[7], "sample": r[8]}
        for r in rows
        if not any(r[0][:24] in submitted for submitted in taken)
    ]  # fmt: skip
    samples = _sample_set(task, unused)
    if len(samples) < task.agreement:
        raise PrometheosError(
            "validation_error",
            "the layer has no accepted proposal (not yet used) for this file's current bytes",
            {"needed": task.agreement, "found": len(samples)},
        )
    return {"task_kind": task.kind, "scan_gen": scan_gen, "subject": subject,
            "input_sha256": bundle.input_sha256, "samples": samples,
            "available": [x["result_key"] for x in unused], "all_samples": unused,
            "assist_kind": task.assist_kind}  # fmt: skip


def _sample_set(task: tasks.Task, unused: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first ``k`` unused samples, from different models when ``k >= 2``."""
    chosen: list[dict[str, Any]] = []
    for x in unused:
        if task.agreement >= 2 and any(c["model"] == x["model"] for c in chosen):
            continue
        chosen.append(x)
        if len(chosen) == task.agreement:
            break
    return chosen


def _set_of(task: tasks.Task, unused: list[dict[str, Any]], key: str) -> list[str]:
    """The sample set of one clicked sample (review cp22-a R5-F2): it, then for each other
    sample index one unused sample of the same bytes from a model not yet chosen, preferring the
    clicked sample's run. Built on the server, so a list the hub truncated cannot change it."""
    clicked = next((x for x in unused if x["result_key"] == key), None)
    if clicked is None:
        return [key]
    chosen = [clicked]
    for index in sorted({x["sample"] for x in unused} - {clicked["sample"]}):
        fresh = [x for x in unused if x["sample"] == index
                 and all(c["model"] != x["model"] for c in chosen)]  # fmt: skip
        same_run = [x for x in fresh if x["run_id"] == clicked["run_id"]]
        if same_run or fresh:
            chosen.append((same_run or fresh)[0])
        if len(chosen) == task.agreement:
            break
    return [x["result_key"] for x in chosen]


def _can_agree(task: tasks.Task, samples: list[dict[str, Any]]) -> None:
    """Refuse, before anything is consumed, a set CP17.4 could never accept (R4-F2)."""
    from prometheos.adoption import assist

    if len(samples) != task.agreement:
        raise PrometheosError(
            "validation_error",
            f"choose {task.agreement} sample(s) for {task.kind}",
            {"needed": task.agreement, "found": len(samples)},
        )
    if task.agreement >= 2 and len({x["model"] for x in samples}) < 2:
        raise PrometheosError(
            "validation_error",
            "the samples come from one model; agreement needs two different models",
            {"needed": task.agreement},
        )
    if not assist.same([x["result"] for x in samples]):
        raise PrometheosError("validation_error", "the chosen samples disagree", {})


def accept_keys(
    svc: Any, project_id: str, *, task_kind: str, scan_gen: int, subject: str,
    result_keys: list[str] | None,
) -> list[str]:  # fmt: skip
    """The exact samples an accept names, fixed where the request is made (review cp22-a R6-F1):
    none named means the preview's set; one clicked key of a ``k >= 2`` task means its set,
    formed here. An agent's approval request stores this full list, so the owner approves the
    samples that will be submitted and execution takes exactly them."""
    shown = accept_preview(svc, project_id, task_kind=task_kind, scan_gen=scan_gen,
                           subject=subject)  # fmt: skip
    task = tasks.get(task_kind)
    if result_keys is None:
        return [x["result_key"] for x in shown["samples"]]
    if len(result_keys) == 1 and task.agreement >= 2:
        return _set_of(task, shown["all_samples"], result_keys[0])
    return list(result_keys)


def accept(
    svc: Any, project_id: str, *, task_kind: str, scan_gen: int, subject: str,
    authority: object, result_keys: list[str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The owner accepts a proposal: its samples enter CP17.4's assist path, which validates
    them again and lets them join the generation as proposed evidence (T4 with a verified
    quote, else T8). Fusion never lets them decide; nothing closes an owner item (A07.7)."""
    from prometheos.adoption import assist

    act = require_owner(authority, "accepting a layer proposal")
    shown = accept_preview(svc, project_id, task_kind=task_kind, scan_gen=scan_gen,
                           subject=subject)  # fmt: skip
    offered = [x["result_key"] for x in shown["samples"]]
    if result_keys is not None and set(result_keys) <= set(shown["available"]):
        # The owner chose these samples (the row clicked, or the approved plan): exactly them.
        chosen = {x["result_key"]: x for x in shown["all_samples"]}
        shown["samples"] = [chosen[k] for k in result_keys]
        offered = list(result_keys)
    if result_keys is not None and result_keys != offered:
        # Exactly the samples the owner approved, not ones stored later (review cp22-a R2-F2).
        raise PrometheosError(
            "revision_conflict",
            "the proposal's samples changed since it was approved; review it again",
            {"approved": result_keys, "now": offered},
        )
    store = svc.open(project_id)
    root = Path(svc.research_root(project_id, _scan_alias(store, scan_gen)))
    conn = store.connect()
    try:
        bundle = assist._bundle(conn, root, scan_gen, shown["assist_kind"], subject)
        taken = len(assist.samples(conn, bundle.assist_id))
    finally:
        conn.close()
    outcomes = []
    _can_agree(tasks.get(task_kind), shown["samples"])
    for offset, sample in enumerate(shown["samples"]):
        done = assist.submit(
            store, root, scan_gen, shown["assist_kind"], subject, taken + offset + 1,
            sample["result"], sample["model"],
            # the sample's key first: the submitter text is cut at 100 characters
            f"{sample['result_key'][:24]} {sample['executor']} accepted by {act.actor}",
        )  # fmt: skip
        outcomes.append(done)
    last = outcomes[-1]
    return {"task_kind": task_kind, "subject": subject, "accepted_samples": len(outcomes),
            "assist_outcome": last["outcome"]["state"],
            "evidence_added": sum(o["evidence_added"] for o in outcomes)}  # fmt: skip


def _scan_alias(store: ProjectStore, scan_gen: int) -> str:
    from prometheos.adoption import tables

    conn = store.connect()
    try:
        return str(tables.require_scan(conn, scan_gen).root_alias)
    finally:
        conn.close()


def results_list(
    svc: Any, project_id: str, task_kind: str | None, limit: int, scan_gen: int | None = None,
    current: bool = False,
) -> dict[str, Any]:  # fmt: skip
    store = svc.open(project_id)
    if not current:
        return {"results": results.listing(store, task_kind=task_kind, limit=limit,
                                           scan_gen=scan_gen)}  # fmt: skip
    # The layer's own answers (decision 0104): measurements of other models and older task
    # versions may far outnumber them, so they are chosen here, not by a newest-first window.
    granted = _in_force(store)
    models: dict[str, list[str]] = {}
    if granted is not None:
        for grant in granted["policy"]["tasks"]:
            models[grant["task_kind"]] = [m["model"] for m in grant["models"]]
    seen: set[tuple[str, str, int, str]] = set()
    out = []
    for row in results.listing(store, task_kind=task_kind, limit=1_000_000, scan_gen=scan_gen):
        kind = row["task_kind"]
        if kind not in tasks.REGISTRY or row["task_version"] != tasks.get(kind).version:
            continue
        if kind in models and row["model"] not in models[kind]:
            continue
        key = (kind, row["subject"], int(row["sample"]), row["model"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) == limit:
            break
    return {"results": out}


def evaluations(svc: Any, project_id: str) -> dict[str, Any]:
    """The latest measurement of each registered task's current version and model (or model
    set), with why it confers no trust when it does not (decision 0104)."""
    from prometheos.intelligence import evaluation

    store = svc.open(project_id)
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT evaluation_id, task_kind, task_version, model, provider_set, executor, "
            "trusted, result, measured_at, record_sha256 FROM ai_evaluations "
            "ORDER BY measured_at DESC, rowid DESC LIMIT 1000"
        ).fetchall()
    finally:
        conn.close()
    seen: set[tuple[str, int, str]] = set()
    out = []
    for r in rows:
        unit = (str(r[1]), int(r[2]), str(r[3]))
        if r[1] not in tasks.REGISTRY or int(r[2]) != tasks.get(str(r[1])).version or unit in seen:
            continue  # unregistered kinds (spikes) and superseded task versions stay in the record
        seen.add(unit)
        result = json.loads(r[7])
        try:
            record: Any = evaluation._fixed(store, str(r[9]))
        except (PrometheosError, ValueError):  # missing, or fixed before decision 0105 (I2)
            record = None
        out.append(
            {"evaluation_id": r[0], "task_kind": r[1], "task_version": r[2], "model": r[3],
             "providers": json.loads(r[4]), "executor": r[5], "trusted": bool(r[6]),
             "coverage": result["coverage"],
             "precision_lower_bound": result["precision_lower_bound"],
             "correct": int(result.get("correct") or 0),
             "answered": int(result.get("answered") or 0), "items": int(result.get("items") or 0),
             "why": evaluation.why_not(result, record), "measured_at": r[8]}
        )  # fmt: skip
        if len(out) == 200:
            break
    return {"evaluations": out}


def digest_of(document: Any) -> str:
    return canonical_sha256(document)


def _record_subjects(kind: str) -> Any:
    """The subjects builder of a record task (decisions 0109, 0110), else ``validation_error``."""
    from prometheos.app import card_layer, map_layer, results_layer

    builders = {
        map_layer.TASK_KIND: map_layer.subjects,
        results_layer.TASK_KIND: results_layer.subjects,
        card_layer.TASK_KIND: card_layer.subjects,
    }
    if kind not in builders:
        raise PrometheosError("validation_error", "not a record task", {"task_kind": kind})
    return builders[kind]


def run_map_task(
    svc: Any,
    project_id: str,
    *,
    task_kind: str,
    claim_ids: list[str] | None,
    agent: bool,
    dry_run: bool = False,
    cassettes: Path | None = None,
    caller: Any = None,
) -> dict[str, Any]:
    """A record task over the records it applies to, inside the standing policy only: L1 on
    unplaced claims (decision 0109), L2 on results waiting for interpretation and L3 on decision
    cards (decision 0110). ``claim_ids`` narrows each to the claims named (for L2 the claims whose
    quantity is drafted, for L3 the cards about them). There is no per-call route for these tasks
    yet, so outside the policy the answer is ``capability_unavailable`` and nothing is sent.
    Without a key the coding agent does the work itself (its links and assessments read as the
    agent's until the owner confirms them)."""
    from prometheos.app.reads import Caller

    task = tasks.get(task_kind)
    builder = _record_subjects(task.kind)
    if claim_ids is not None and not 1 <= len(claim_ids) <= 500:
        raise PrometheosError("validation_error", "name between 1 and 500 claims", {})
    store = svc.open(project_id)
    who = caller if caller is not None else Caller(actor="cli", scope="owner")
    from prometheos.app import results_layer

    if task.kind == results_layer.TASK_KIND:  # file lines leave: the file's sensitivity counts
        subjects = builder(store, who, claim_ids, root_levels(svc, project_id))
    else:
        subjects = builder(store, who, claim_ids)
    if cassettes is not None:
        executor: Any = CassetteExecutor.from_folder(cassettes)
        grant = None
    else:
        conn = store.connect()
        try:
            config, revision = provider_config.load(conn)
        finally:
            conn.close()
        try:
            grant = ai_policy.check_run(
                _in_force(store), config, revision, task_kind=task.kind, sensitivity="normal",
                agent=agent,
            )  # fmt: skip
        except ai_policy.OutsidePolicy as outside:
            raise PrometheosError(
                "capability_unavailable",
                f"{outside.message}. Record tasks run only inside the standing policy; grant "
                f"one that lists {task.kind}, or let your coding agent do it",
                {"capability": f"intelligence.{task.kind}"[:64], "blocker": "not_configured"},
            ) from None
        _distinct_models(task, grant)
        granted = _in_force(store)
        assert granted is not None
        run_cap = Decimal(granted["policy"]["run_cap_usd"])
        gateway = Gateway(config, svc._secrets(), svc._transport(), svc.home, project_id)
        guard = _guard(svc, project_id, revision, granted["policy_sha256"])
        executor = GatewayExecutor(store, gateway, grant, run_cap, guard)
    if dry_run:
        normal = [x for x in subjects if x.sensitivity == "normal"]
        pairs = list(grant.models) if grant else []
        upper = Decimal(0)
        for x in normal:
            system, user = task.messages(x.bundle.input)
            upper += sum(
                (estimate_cost(m, len(system) + len(user), task.max_output_tokens) for m in pairs),
                Decimal(0),
            ) * task.agreement  # fmt: skip
        return {
            "dry_run": True,
            "task_kind": task.kind,
            "subjects": len(subjects),
            "denied": len(subjects) - len(normal),
            "skipped": [],
            "calls_at_most": len(normal) * task.agreement * len(pairs),
            "estimate_upper_usd": str(upper),
            "estimate_label": "at most: every sample of every subject through every evaluated "
            "pair, each input character counted as a token; no project content is sent (the "
            "key's remaining limit is read)",
            "spend": spend.summary(svc.home),
            "key": executor.gateway.key_limit() if grant else None,
        }
    if grant is not None:
        _cap_by_key(executor)
    return run(store, task, subjects, executor)
