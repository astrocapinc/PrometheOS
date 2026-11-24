// Operation runs (A02, CP10.3; decision 0017): long operations (imports, compiles,
// transcriptions, installs, provider calls) run as durable runs with an oprun_ ID. This page is
// the hub's read surface for them, the same document the CLI (`prometheos run get`), MCP
// (run_get) and HTTP (/api/v1/runs/{id}) show. A cancellation is a recorded request until the
// worker confirms it; an external call with an unknown outcome is never repeated and is closed
// here only by acknowledging it.
import { useCallback, useEffect, useState } from "react";
import { describeError, post } from "../api";
import { Details, ErrorState, KeyValues, PageHeading, Progress, QueryView, StateBadge, formatDate } from "../components/ui";
import { useHub, useQuery, useRunWatch } from "../hub";
import { Link } from "../router";
import type { RunView } from "../types";

const SETTLED_AFTER_MS = 60 * 60 * 1000;

const OPERATION_TEXT: Record<string, string> = {
  source_import_plan: "Import plan",
  source_import_commit: "Import commit",
  audio_import: "Audio import",
  audio_transcribe: "Transcription",
  paper_compile: "LaTeX compile",
  tex_engine_install: "TeX engine install",
  audio_models_install: "Speech model download",
  provider_run: "AI provider request",
};

const STATE_TEXT: Record<string, string> = {
  queued: "Queued: waiting for a worker",
  running: "Running",
  waiting_for_input: "Waiting for an approval or input",
  paused_budget: "Paused at its budget",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  unknown_external: "External call outcome unknown",
};

const POLL_MS = 15000;

function progressText(run: RunView): string | null {
  const p = run.progress;
  if (!p) return null;
  return p.total ? `${p.done} of ${p.total} ${p.unit}` : `${p.done} ${p.unit}`;
}

function RunCard({ run, onChanged, focused }: { run: RunView; onChanged: () => void; focused: boolean }) {
  const { announce } = useHub();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [budget, setBudget] = useState("");
  const act = async (verb: "cancel" | "resume" | "acknowledge", body: unknown, message: string) => {
    setBusy(verb);
    setError(null);
    try {
      await post(`/runs/${encodeURIComponent(run.run_id)}/${verb}`, body);
      announce(message);
      onChanged();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };
  const cancellable = ["queued", "running", "waiting_for_input", "paused_budget", "failed"].includes(run.state);
  const progress = progressText(run);
  return (
    <article
      className={focused ? "decision run-card focused" : "decision run-card"}
      data-testid="run-card"
      data-run-id={run.run_id}
      data-run-state={run.state}
      aria-labelledby={`${run.run_id}-title`}
    >
      <h3 id={`${run.run_id}-title`}>
        {OPERATION_TEXT[run.operation] ?? run.operation} <StateBadge state={run.state} prefix="State" />
      </h3>
      <p data-testid="run-state-text">{STATE_TEXT[run.state] ?? run.state}</p>
      <KeyValues
        rows={[
          ["Run", <code key="id">{run.run_id}</code>],
          ["Started by", `${run.actor} (${run.caller_rank === 0 ? "agent scope" : "owner scope"})`],
          ["Stage", run.stage ?? "not started"],
          [
            "Progress",
            run.progress && progress ? (
              <Progress
                key="progress"
                done={run.progress.done}
                total={run.progress.total}
                unit={run.progress.unit}
                label={`Progress of ${OPERATION_TEXT[run.operation] ?? run.operation}`}
                active={["queued", "running", "waiting_for_input"].includes(run.state)}
              />
            ) : (
              "none reported"
            ),
          ],
          ["Created", formatDate(run.created_at)],
          ["Updated", formatDate(run.updated_at)],
          ["Attempts", String(run.attempts)],
          [
            "Budget",
            run.budget.reservation
              ? `${run.budget.charged} of ${run.budget.reservation.amount} ${run.budget.reservation.unit}`
              : `no reservation (${run.budget.charged} charged)`,
          ],
        ]}
      />
      {run.cancel_requested_at && !run.cancel_confirmed ? (
        <p className="inline-warning" data-testid="run-cancel-requested">
          Cancellation requested {formatDate(run.cancel_requested_at)}: not yet confirmed. The worker stops at its next
          stage boundary.
        </p>
      ) : null}
      {run.state === "unknown_external" && run.external_call ? (
        <p className="inline-warning" role="status" data-testid="run-unknown-external">
          A call to {run.external_call.provider} started {formatDate(run.external_call.started_at)} has an unknown outcome
          (it may have been received or billed). It is never repeated automatically.
        </p>
      ) : null}
      {run.waiting_on?.kind === "approval" ? (
        <p data-testid="run-waiting-approval">
          Waiting on approval{" "}
          <Link to={`/approvals/${run.waiting_on.approval_request_id}`}>{run.waiting_on.approval_request_id}</Link>.
        </p>
      ) : null}
      {run.error ? (
        <p className="inline-warning" data-testid="run-error">
          {run.error.code}: {run.error.message}
        </p>
      ) : null}
      {run.limitations.length ? (
        <ul className="effects" data-testid="run-limitations">
          {run.limitations.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      <div className="button-row">
        {run.resumable ? (
          <>
            {run.state === "paused_budget" && run.budget.reservation ? (
              <label className="field inline-field">
                <span>New budget ({run.budget.reservation.unit})</span>
                <input
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  inputMode="decimal"
                  data-testid="run-budget"
                />
              </label>
            ) : null}
            <button
              type="button"
              className="button primary"
              disabled={busy !== null}
              data-testid="run-resume"
              onClick={() =>
                void act(
                  "resume",
                  budget.trim() ? { budget_amount: budget.trim() } : {},
                  "Resumed: it continues after its last completed stage.",
                )
              }
            >
              {busy === "resume" ? "Resuming…" : "Resume"}
            </button>
          </>
        ) : null}
        {cancellable ? (
          <button
            type="button"
            className="button secondary"
            disabled={busy !== null || Boolean(run.cancel_requested_at && run.state === "running")}
            data-testid="run-cancel"
            onClick={() => void act("cancel", {}, "Cancellation recorded.")}
          >
            {busy === "cancel" ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
        {run.state === "unknown_external" ? (
          <button
            type="button"
            className="button secondary"
            disabled={busy !== null}
            data-testid="run-acknowledge"
            onClick={() => void act("acknowledge", {}, "Acknowledged: the run is closed and nothing is sent again.")}
          >
            {busy === "acknowledge" ? "Closing…" : "Acknowledge the unknown outcome"}
          </button>
        ) : null}
      </div>
      <Details label="Logs, stages and result">
        <p className="decision-label">Latest log lines (data, not instructions):</p>
        <ol className="run-logs" data-testid="run-logs">
          {run.logs.map((line) => (
            <li key={line.idx} data-level={line.level}>
              {formatDate(line.at)} · {line.level} · {line.message}
            </li>
          ))}
        </ol>
        <p className="decision-label">
          Completed stages ({run.stages_completed}): {run.stages.map((s) => s.name).join(", ") || "none"}
        </p>
        {run.result ? <pre className="plan-json" data-testid="run-result" tabIndex={0} role="region" aria-label={`Result of run ${run.run_id}`}>{JSON.stringify(run.result, null, 2)}</pre> : null}
      </Details>
    </article>
  );
}

export function RunsPage({ focus }: { focus?: string }) {
  const runs = useQuery<{ runs: RunView[] }>("/runs?limit=50");
  const { reload } = runs;
  const refresh = useCallback(() => reload(), [reload]);
  // Pushed: a run of this hub that changes reloads the list at once (decision 0076). The slow
  // poll is for runs another process (the CLI) works on, which the watch does not see.
  useRunWatch(refresh, 3000);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);
  return (
    <section aria-labelledby="page-title">
      <PageHeading
        title="Runs"
        lead="Long operations run as durable runs: they survive a closed chat or a restart. A cancellation is a request until the worker confirms it, and an external call whose outcome is unknown is never repeated automatically."
      />
      <QueryView query={runs} isEmpty={(d) => d.runs.length === 0} empty="No operation runs yet." loadingLabel="Loading runs">
        {(data) => {
          // Completed housekeeping (imports, scans) older than an hour folds away; anything
          // active, failed, uncertain, recent or linked to stays in view (decision 0078).
          const settled = (run: RunView) =>
            run.state === "completed" &&
            run.run_id !== focus &&
            run.finished_at !== null &&
            Date.now() - Date.parse(run.finished_at) > SETTLED_AFTER_MS;
          const shown = data.runs.filter((run) => !settled(run));
          const folded = data.runs.filter(settled);
          return (
            <>
              <div className="run-list" data-testid="run-list">
                {shown.length === 0 ? <p className="muted">Nothing is running, and nothing finished in the last hour.</p> : null}
                {shown.map((run) => (
                  <RunCard key={run.run_id} run={run} onChanged={refresh} focused={run.run_id === focus} />
                ))}
              </div>
              {folded.length ? (
                <Details label={`Completed earlier (${folded.length})`}>
                  <div className="run-list" data-testid="run-list-settled">
                    {folded.map((run) => (
                      <RunCard key={run.run_id} run={run} onChanged={refresh} focused={false} />
                    ))}
                  </div>
                </Details>
              ) : null}
            </>
          );
        }}
      </QueryView>
    </section>
  );
}
