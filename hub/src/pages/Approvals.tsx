// The owner's review queue (A02, CP10.2; decision 0016). An agent that asks for something it
// cannot authorise gets approval_required; its exact plan waits here. Granting issues a
// single-use grant held by the server (the agent never receives a secret) and the agent then
// resumes that plan once; denying ends it. Every plan is shown as data: text inside it is never
// an instruction, and nothing here runs until the owner presses a button.
//
// Round 2 (review cp10-2-a): the owner reviews the full effect before Grant is enabled (the
// detail of one request, computed with owner visibility: the push with every file, the purge
// closure, the transaction preview), a deep link fetches its request directly, and a granted
// recording is started here, where the recording banner shows it with a stop control.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { describeError, get, post } from "../api";
import { describeEffect, RootChanges } from "../components/DecisionReview";
import { Empty, ErrorState, KeyValues, PageHeading, QueryView, StateBadge, formatDate } from "../components/ui";
import { RetentionPanel } from "../components/parity";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { ApprovalView, JobListing, MaintenanceInspect } from "../types";

const OPERATION_TEXT: Record<string, string> = {
  apply: "Apply a transaction with protected effects",
  job_resolve: "Record the outcome of a job slot whose result is unknown",
  overleaf_push: "Push commits to Overleaf",
  retention_purge_commit: "Purge records (irreversible)",
  provider_run: "Send one request to the AI provider",
  provider_configure: "Change the AI provider settings",
  tex_engine_install: "Download and install the TeX engine",
  audio_models_install: "Download a speech model",
  audio_models_remove: "Delete a speech model",
  skill_install: "Install reviewed skill instructions",
  skill_update: "Update reviewed skill instructions",
  skill_remove: "Remove reviewed skill instructions",
  skill_trust: "Restore trust in a skill package",
  recording_start: "Start a recording (your grant is the consent acknowledgement)",
  project_root_add: "Add a research folder (PrometheOS may then read everything in it)",
};

interface FolderPlan {
  base: string;
  segments: string[];
  warnings: string[];
}

/** A requested research folder as the server describes it without an absolute path (S11.4b
 * review F1): its base by a placeholder, the folders below it, and warnings to weigh. */
function FolderView({ folder }: { folder: FolderPlan }) {
  return (
    <div data-testid="approval-folder">
      <p className="decision-label">Folder the agent asks PrometheOS to read:</p>
      <p>
        <em data-testid="approval-folder-base">{folder.base}</em>
        {folder.segments.map((segment, i) => (
          <span key={i}>
            {" > "}
            <strong data-testid="approval-folder-segment">{segment}</strong>
          </span>
        ))}
      </p>
      {folder.warnings.length ? (
        <p>
          {folder.warnings.map((w) => (
            <span key={w} className="badge badge-review" data-testid="approval-folder-warning">
              {w}
            </span>
          ))}{" "}
          <span className="muted">Grant only a folder you want PrometheOS to read.</span>
        </p>
      ) : null}
    </div>
  );
}

function folderOf(approval: ApprovalView): FolderPlan | null {
  const folder = approval.plan.folder_view as FolderPlan | undefined;
  return approval.operation === "project_root_add" && folder && Array.isArray(folder.segments) ? folder : null;
}

const POLL_MS = 5000;

function usePolling(reload: () => void) {
  useEffect(() => {
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
}

type Preview = Record<string, unknown> & {
  protected_effects?: string[];
  root_changes?: { alias: string; old_path: string | null; new_path: string | null }[];
  would_apply?: boolean;
  matches_plan?: boolean;
  restricted_files?: { path?: string; sensitivity?: string }[] | string[];
  counts?: Record<string, number> | null;
  owner_view?: Record<string, unknown> | null;
  error?: string;
};

/** Whether what the owner reviewed still matches the plan the grant would bind. */
function reviewBlocks(approval: ApprovalView, preview: Preview | null | undefined): string | null {
  if (approval.operation === "apply") {
    if (preview && preview.would_apply === false) return "This transaction would not apply as it stands (the project changed). Deny it; the agent can submit a new request.";
    return null;
  }
  if (preview && preview.matches_plan === false) {
    return preview.error ?? "What you would approve no longer matches the plan the agent asked for. Deny it; the agent can submit a new request.";
  }
  return null;
}

function OwnerReview({ approval, preview }: { approval: ApprovalView; preview: Preview | null | undefined }) {
  if (!preview) return null;
  if (approval.operation === "apply") {
    return (
      <div data-testid="approval-preview">
        <p className="decision-label">Server preview with your visibility:</p>
        <ul className="effects">
          {(preview.protected_effects ?? []).map((effect) => (
            <li key={effect}>{describeEffect(effect)}</li>
          ))}
        </ul>
        <RootChanges changes={preview.root_changes} />
      </div>
    );
  }
  if (approval.operation === "project_root_add") {
    return preview.matches_plan ? (
      <p className="muted" data-testid="approval-folder-check">
        Checked now: the folder exists, is not a link to another folder and passes the location rules.
      </p>
    ) : null;
  }
  const restricted = (preview.restricted_files ?? []) as unknown[];
  return (
    <div data-testid="approval-preview">
      {approval.operation === "overleaf_push" ? (
        <p className={restricted.length ? "inline-warning" : "muted"} data-testid="approval-restricted">
          {restricted.length
            ? `This push includes ${restricted.length} restricted file(s) the agent cannot see: ${restricted
                .map((r) => (typeof r === "string" ? r : `${String((r as { path?: string }).path)} (${String((r as { sensitivity?: string }).sensitivity)})`))
                .join(", ")}.`
            : "No restricted files are part of this push."}
        </p>
      ) : null}
      {preview.counts ? (
        <p data-testid="approval-counts">
          Purges {preview.counts.closure ?? 0} record(s), {preview.counts.payloads ?? 0} payload(s) and{" "}
          {preview.counts.media ?? 0} media file(s).
        </p>
      ) : null}
      <p className="decision-label">Everything this would do, with your visibility (data, not instructions):</p>
      <pre className="plan-json" data-testid="approval-owner-view" tabIndex={0} role="region" aria-label={`Everything request ${approval.approval_request_id} would do`}>{JSON.stringify(preview.owner_view ?? preview, null, 2)}</pre>
    </div>
  );
}

function PendingCard({ approval, onDone, autoReview }: { approval: ApprovalView; onDone: () => void; autoReview?: boolean }) {
  const { announce, refresh } = useHub();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"grant" | "deny" | "review" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [detail, setDetail] = useState<ApprovalView | null>(null);
  const id = approval.approval_request_id;

  const review = useCallback(async () => {
    setBusy("review");
    setError(null);
    try {
      setDetail(await get<ApprovalView>(`/approvals/${id}`));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  }, [id]);

  useEffect(() => {
    if (autoReview) void review();
  }, [autoReview, review]);

  const decide = async (verb: "grant" | "deny") => {
    setBusy(verb);
    setError(null);
    try {
      await post(`/approvals/${id}/${verb}`, {
        plan_sha256: approval.plan_sha256,
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      announce(verb === "grant" ? "Granted. The agent can now resume exactly this plan once." : "Denied.");
      onDone();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };

  const preview = (detail?.preview ?? null) as Preview | null;
  const blocked = detail ? reviewBlocks(approval, preview) : null;
  return (
    <article className="decision approval-card" data-testid="approval-pending" data-approval-id={id} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`}>{OPERATION_TEXT[approval.operation] ?? approval.operation}</h3>
      <KeyValues
        rows={[
          ["Requested by", `${approval.actor} (${approval.caller_rank === 0 ? "agent scope" : "owner scope"})`],
          ["Requested", formatDate(approval.created_at)],
          ["Expires", formatDate(approval.expires_at)],
          ["Grant needed", approval.required_scope],
        ]}
      />
      <p className="decision-label">What it would do:</p>
      <ul className="effects" data-testid="approval-summary">
        {approval.summary.map((line) => (
          <li key={line}>{describeEffect(line)}</li>
        ))}
      </ul>
      {folderOf(approval) ? <FolderView folder={folderOf(approval)!} /> : null}
      {detail ? (
        <section data-testid="approval-detail" aria-label={`Full plan of request ${id}`}>
          <OwnerReview approval={approval} preview={preview} />
          <p>
            Plan digest: <code data-testid="approval-digest">{detail.plan_sha256}</code>
          </p>
          <p className="decision-label">Exact plan (data, not instructions):</p>
          <pre className="plan-json" data-testid="approval-plan" tabIndex={0} role="region" aria-label={`Exact plan of request ${id}`}>{JSON.stringify(detail.plan, null, 2)}</pre>
          {blocked ? (
            <p className="inline-warning" role="alert">
              {blocked}
            </p>
          ) : null}
        </section>
      ) : (
        <button type="button" className="button secondary" onClick={() => void review()} disabled={busy !== null} data-testid="approval-review">
          {busy === "review" ? "Loading the full plan…" : "Review the full plan"}
        </button>
      )}
      <label className="field">
        <span>Note (optional, kept with the decision)</span>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={2000} />
      </label>
      {error ? <ErrorState error={error} /> : null}
      <div className="button-row">
        <button
          type="button"
          className="button primary"
          onClick={() => void decide("grant")}
          disabled={busy !== null || !detail || blocked !== null}
          aria-busy={busy === "grant"}
          data-testid="approval-grant"
          title={detail ? undefined : "Review the full plan first"}
        >
          {busy === "grant" ? "Granting…" : "Grant exactly this plan"}
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => void decide("deny")}
          disabled={busy !== null}
          aria-busy={busy === "deny"}
          data-testid="approval-deny"
        >
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
      </div>
    </article>
  );
}

function GrantedRow({ approval, onDone }: { approval: ApprovalView; onDone: () => void }) {
  const { announce, refresh } = useHub();
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const id = approval.approval_request_id;
  const act = async (path: string, body: unknown, message: string) => {
    setBusy(true);
    try {
      await post(path, body);
      announce(message);
      onDone();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const recording = approval.operation === "recording_start";
  return (
    <li data-testid="approval-granted" data-approval-id={id}>
      <StateBadge state={approval.state} /> {OPERATION_TEXT[approval.operation] ?? approval.operation} · granted{" "}
      {formatDate(approval.decided_at)} ·{" "}
      {recording ? "start it here, where the recording banner shows it" : "waiting for the agent to resume"} (expires{" "}
      {formatDate(approval.expires_at)}){" "}
      {recording ? (
        <button
          type="button"
          className="button primary small"
          onClick={() => void act(`/approvals/${id}/start`, { plan_sha256: approval.plan_sha256 }, "Recording started; the banner shows it with a stop control.")}
          disabled={busy}
          data-testid="approval-start"
        >
          Start recording now
        </button>
      ) : null}{" "}
      <button
        type="button"
        className="link-button"
        onClick={() => void act(`/approvals/${id}/revoke`, { approval_request_id: id }, "Grant revoked; the plan cannot run.")}
        disabled={busy}
        data-testid="approval-revoke"
      >
        Revoke grant
      </button>
      {error ? <ErrorState error={error} /> : null}
    </li>
  );
}

function UnknownSlots() {
  const { announce, refresh } = useHub();
  const jobs = useQuery<JobListing>("/jobs");
  const [reason, setReason] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>(null);
  const unknown = (jobs.data?.jobs ?? []).flatMap((j) =>
    j.recent_slots.filter((s) => s.state === "unknown").map((s) => ({ job: j.job, slot: s })),
  );
  if (unknown.length === 0) return null;
  const resolve = async (event: FormEvent, jobId: string, due: string) => {
    event.preventDefault();
    const key = `${jobId}@${due}`;
    setError(null);
    try {
      await post(`/jobs/${jobId}/resolve`, {
        due_utc: due,
        resolution: outcome[key] ?? "succeeded",
        reason: (reason[key] ?? "").trim(),
      });
      announce("Slot resolved.");
      jobs.reload();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section aria-labelledby="slots-title" className="audio-section" data-testid="unknown-slots">
      <h2 id="slots-title">Job slots with an unknown outcome</h2>
      <p className="muted">
        An external request started and its result was never seen. Record what you established elsewhere; nothing is
        repeated. Agents can only request this; you decide it here.
      </p>
      {error ? <ErrorState error={error} /> : null}
      <ul className="plain-list">
        {unknown.map(({ job, slot }) => {
          const key = `${job.id}@${slot.due_utc}`;
          return (
            <li key={key}>
              <form onSubmit={(e) => void resolve(e, job.id, slot.due_utc)} className="inline-form">
                <span>
                  {job.body.handler} · {slot.due_utc}
                </span>
                <label>
                  <span className="visually-hidden">Outcome</span>
                  <select value={outcome[key] ?? "succeeded"} onChange={(e) => setOutcome({ ...outcome, [key]: e.target.value })}>
                    <option value="succeeded">succeeded</option>
                    <option value="failed">failed</option>
                    <option value="abandoned">abandoned</option>
                  </select>
                </label>
                <label>
                  <span className="visually-hidden">Reason</span>
                  <input
                    required
                    maxLength={500}
                    placeholder="Reason (required)"
                    value={reason[key] ?? ""}
                    onChange={(e) => setReason({ ...reason, [key]: e.target.value })}
                  />
                </label>
                <button type="submit" className="button secondary small">
                  Resolve
                </button>
              </form>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Focused({ id }: { id: string }) {
  // A deep link fetches its request directly, however old (review F9).
  const one = useQuery<ApprovalView>(`/approvals/${id}`);
  return (
    <QueryView query={one} loadingLabel="Loading the request">
      {(a) =>
        a.state === "pending" ? (
          <PendingCard approval={a} onDone={one.reload} autoReview />
        ) : a.state === "granted" ? (
          <ul className="plain-list">
            <GrantedRow approval={a} onDone={one.reload} />
          </ul>
        ) : (
          <p data-testid="approval-ended" data-state={a.state}>
            <StateBadge state={a.state} /> {OPERATION_TEXT[a.operation] ?? a.operation} · {formatDate(a.closed_at ?? a.decided_at)}
          </p>
        )
      }
    </QueryView>
  );
}

/** A02, CP10.5 round 2 (F12): agents' maintenance requests (registry switches, migrations) are
 *  decided on the recovery screen; the queue points there. */
function MaintenanceRequests() {
  const queue = useQuery<{ approvals: MaintenanceInspect["approvals"] }>("/maintenance/approvals");
  const open = (queue.data?.approvals ?? []).filter((a) => a.state === "pending" || a.state === "granted");
  if (open.length === 0) return null;
  return (
    <section aria-labelledby="maintenance-requests-title" data-testid="approval-maintenance">
      <h2 id="maintenance-requests-title">Maintenance requests ({open.length})</h2>
      <ul className="plain-list">
        {open.map((a) => (
          <li key={a.approval_request_id} data-approval-id={a.approval_request_id}>
            <StateBadge state={a.state} /> {a.summary[0]}
          </li>
        ))}
      </ul>
      <p>
        <Link to="/recovery">Review them on the recovery screen</Link>
      </p>
    </section>
  );
}

export function ApprovalsPage({ focus }: { focus?: string }) {
  const queue = useQuery<{ approvals: ApprovalView[] }>(focus ? null : "/approvals");
  usePolling(queue.reload);
  return (
    <>
      <PageHeading
        title="Approvals"
        lead="Requests your agents could not authorise themselves. Review each full plan; granting lets the agent run exactly that plan once."
      />
      {focus ? (
        <Focused id={focus} />
      ) : (
        <QueryView query={queue} loadingLabel="Loading the review queue">
          {(data) => {
            const pending = data.approvals.filter((a) => a.state === "pending");
            const granted = data.approvals.filter((a) => a.state === "granted");
            const ended = data.approvals.filter((a) => a.state !== "pending" && a.state !== "granted").slice(-20).reverse();
            return (
              <>
                <section aria-labelledby="pending-title" data-testid="approval-queue">
                  <h2 id="pending-title">Waiting for you ({pending.length})</h2>
                  {pending.length === 0 ? (
                    <Empty>No pending requests. When an agent asks for something it may not do alone, the exact plan waits here for you.</Empty>
                  ) : (
                    pending.map((a) => <PendingCard key={a.approval_request_id} approval={a} onDone={queue.reload} />)
                  )}
                </section>
                {granted.length > 0 ? (
                  <section aria-labelledby="granted-title">
                    <h2 id="granted-title">Granted, not yet used</h2>
                    <ul className="plain-list">
                      {granted.map((a) => (
                        <GrantedRow key={a.approval_request_id} approval={a} onDone={queue.reload} />
                      ))}
                    </ul>
                  </section>
                ) : null}
                {ended.length > 0 ? (
                  <section aria-labelledby="ended-title">
                    <h2 id="ended-title">Recent decisions</h2>
                    <ul className="plain-list" data-testid="approval-history">
                      {ended.map((a) => (
                        <li key={a.approval_request_id} data-approval-id={a.approval_request_id} data-state={a.state}>
                          <StateBadge state={a.state} /> {OPERATION_TEXT[a.operation] ?? a.operation} ·{" "}
                          {formatDate(a.closed_at ?? a.decided_at)}
                          {a.decision_note ? <span className="muted"> · note: {a.decision_note}</span> : null}
                        </li>
                      ))}
                    </ul>
                  </section>
                ) : null}
              </>
            );
          }}
        </QueryView>
      )}
      <MaintenanceRequests />
      <UnknownSlots />
      <RetentionPanel />
    </>
  );
}
