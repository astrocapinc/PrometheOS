// Decision cards and planned checks on Now (A06.1, CP20.3; decision 0084). A card is an approval
// request the agent attached reasons to: what, why, the recommendation, and what views show if
// you don't answer. "Apply all recommended" previews the listed plans as one request and applies
// it, after your confirmation, in one transaction. Planned checks come from PrometheOS's detector;
// the plan says which ones your agent may start inside your verification budget. CP20.4 (gap audit
// #16): you change that budget here, after a preview of exactly what changes, and a check whose
// cause is gone says so.
import { useState, type FormEvent } from "react";
import { describeError, post } from "../api";
import { Details, Empty, ErrorState, QueryView, formatDate } from "./ui";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type {
  DecisionCardsView,
  DecisionPreview,
  PlannedCheck,
  RecommendedPreview,
  VerificationPlanView,
} from "../types";

const KIND_TEXT: Record<string, string> = {
  read_only: "read-only",
  rerun: "rerun",
  model_edit: "model edit",
  literature: "literature",
};

export function DecisionCards({ onChanged }: { onChanged: () => void }) {
  const cards = useQuery<DecisionCardsView>("/decisions/cards");
  const [preview, setPreview] = useState<RecommendedPreview | null>(null);
  const [single, setSingle] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const ids = cards.data?.recommended ?? [];
  // One card or all of them go through the same preview and apply (decision 0107): the request is
  // rebuilt at the current state, so a card stays answerable after unrelated writes, and nothing is
  // applied when something it changes moved.
  const review = async (chosen: string[] = ids, one = false) => {
    setError(null);
    setDone(null);
    setSingle(one);
    try {
      setPreview(await post<RecommendedPreview>("/decisions/recommended/preview", { approval_request_ids: chosen }));
    } catch (err) {
      setError(err);
    }
  };
  const apply = async () => {
    if (!preview) return;
    setError(null);
    try {
      await post("/decisions/recommended/apply", {
        approval_request_ids: preview.approval_request_ids,
        digest: preview.preview.digest,
      });
      setDone(`Applied ${preview.approval_request_ids.length} recommendation(s) together.`);
      setPreview(null);
      cards.reload();
      onChanged();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section className="section decisions" aria-labelledby="cards-title" data-testid="decision-cards">
      <h2 id="cards-title">Decisions waiting for you</h2>
      <QueryView query={cards} loadingLabel="Loading decisions">
        {(data) =>
          data.cards.length === 0 ? (
            <Empty>No decision is waiting for you.</Empty>
          ) : (
            <>
              <ul className="card-list">
                {data.cards.map((c) => (
                  <li key={c.approval_request_id} className="card decision-card" data-testid="decision-card">
                    <p className="card-title">{c.card.what}</p>
                    <p>
                      <strong>Recommended:</strong> {c.card.recommendation}
                    </p>
                    <p data-testid="card-default">
                      <strong>If you don't answer:</strong> {c.card.default_text}
                      {c.card.default_at
                        ? c.default_state === "default_in_effect"
                          ? " (in effect in views since " + formatDate(c.card.default_at) + ")"
                          : " (from " + formatDate(c.card.default_at) + ")"
                        : null}
                    </p>
                    {c.wording ? (
                      <div className="card-wording" data-testid="card-wording">
                        <p className="lead-label">In plain words (written by the intelligence layer; a hint)</p>
                        <p>{c.wording.what}</p>
                        <p>
                          <strong>Why:</strong> {c.wording.why}
                        </p>
                        <p>
                          <strong>Recommended:</strong> {c.wording.recommendation}
                        </p>
                        <p className="muted small-print">
                          PrometheOS fills in every number from the bound quantities. What happens if you don't
                          answer stays your agent's words above; the layer never changes it.
                        </p>
                      </div>
                    ) : null}
                    {c.plan_current ? (
                      <p className="muted">Only views change; nothing is applied until you approve it.</p>
                    ) : (
                      <p className="warning" data-testid="card-stale">
                        Something it changes was edited since: this plan no longer applies. Deny it and ask your agent
                        again.
                      </p>
                    )}
                    <Details label="Why it matters">
                      <p>{c.card.why}</p>
                      {c.claims.length ? (
                        <ul>
                          {c.claims.map((cl) => (
                            <li key={cl.claim_id}>
                              <q>{cl.line}</q>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      <p className="muted">What approving it does (recorded by PrometheOS):</p>
                      <ul>
                        {c.protected_effects.map((e) => (
                          <li key={e}>{e}</li>
                        ))}
                      </ul>
                      <p className="muted">The card's words are written by your agent ({c.requested_by}).</p>
                    </Details>
                    {c.plan_current && ids.includes(c.approval_request_id) ? (
                      <button
                        type="button"
                        className="button primary small"
                        data-testid="apply-this-card"
                        onClick={() => void review([c.approval_request_id], true)}
                      >
                        Apply this recommendation
                      </button>
                    ) : null}{" "}
                    <Link to={`/approvals/${c.approval_request_id}`} className="button secondary small">
                      Review this one
                    </Link>
                  </li>
                ))}
              </ul>
              {preview ? (
                <div className="stage-confirm" data-testid="recommended-preview">
                  <p>
                    Apply {single ? "this recommendation" : `these ${preview.approval_request_ids.length} recommendations in one step`} ({preview.operations}{" "}
                    change{preview.operations === 1 ? "" : "s"}). If any of them changed since, nothing is applied.
                  </p>
                  <p className="lead-label">What approving the set does (recorded by PrometheOS)</p>
                  <ul data-testid="recommended-effects">
                    {preview.preview.protected_effects.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                  <button type="button" className="button primary small" onClick={() => void apply()}>
                    {single ? "Confirm: apply it" : "Confirm: apply all"}
                  </button>{" "}
                  <button type="button" className="button secondary small" onClick={() => setPreview(null)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="button primary small"
                  data-testid="apply-recommended"
                  onClick={() => void review(ids)}
                >
                  Apply all recommended ({ids.length})
                </button>
              )}
            </>
          )
        }
      </QueryView>
      {done ? (
        <p className="info" role="status">
          {done}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </section>
  );
}

function summary(all: VerificationPlanView): string {
  const data = { ...all, checks: all.checks.filter((c) => !c.resolved) };
  const total = data.checks.reduce((sum, c) => sum + (c.estimated_minutes ?? 0), 0);
  const mine = data.checks.filter((c) => c.eligible);
  const minutes = mine.reduce((sum, c) => sum + (c.estimated_minutes ?? 0), 0);
  const days = Math.ceil(minutes / Math.max(1, data.budget.max_minutes_per_day));
  const needYou = data.checks.length - mine.length;
  return (
    `${data.checks.length} check${data.checks.length === 1 ? "" : "s"}, about ${total} min in all. ` +
    `Your agent may run ${mine.length} within your budget` +
    (mine.length ? ` (about ${days} day${days === 1 ? "" : "s"} at ${data.budget.max_minutes_per_day} min a day)` : "") +
    (needYou ? `; ${needYou} need${needYou === 1 ? "s" : ""} you.` : ".")
  );
}

function CheckList({ checks }: { checks: PlannedCheck[] }) {
  return (
    <ul className="plain-list checks-list">
      {checks.map((c) => (
        <li
          key={c.action_id}
          data-testid="planned-check"
          data-action-id={c.action_id}
          data-eligible={c.eligible ? "yes" : "no"}
          data-resolved={c.resolved ? "yes" : "no"}
        >
          {c.claim_line ? <q>{c.claim_line}</q> : c.title}
          <span className="muted">
            {" · "}
            {KIND_TEXT[c.kind] ?? c.kind}
            {c.estimated_minutes !== null ? `, about ${c.estimated_minutes} min` : ", time unknown"}
            {" · "}
            {c.resolved ? (
              <span data-testid="check-resolved">
                its cause is gone; you can <Link to={`/actions/${c.action_id}`}>close it</Link>
              </span>
            ) : c.eligible ? (
              "your agent may run it"
            ) : (
              `needs you: ${c.why}`
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function rootAliases(settings: unknown): string[] {
  const roots = (settings as { allowed_roots?: { alias: string }[] } | undefined)?.allowed_roots;
  return (roots ?? []).map((r) => r.alias);
}

type Budget = VerificationPlanView["budget"];

/** The owner's budget control: the same settings change as `prometheos verify budget`, shown as
 * PrometheOS's own protected effect before anything is applied. */
function BudgetForm({ budget, onApplied }: { budget: Budget; onApplied: () => void }) {
  const { project, announce, refresh } = useHub();
  const aliases = rootAliases(project?.settings);
  const [each, setEach] = useState(String(budget.max_minutes_each));
  const [day, setDay] = useState(String(budget.max_minutes_per_day));
  const [alias, setAlias] = useState(budget.output_root_alias ?? "");
  const [path, setPath] = useState(budget.output_path ?? "");
  const [preview, setPreview] = useState<DecisionPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const wanted = () => ({
    max_minutes_each: Number(each),
    max_minutes_per_day: Number(day),
    output_root_alias: alias && path.trim() ? alias : null,
    output_path: alias && path.trim() ? path.trim() : null,
  });
  const review = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setPreview(null);
    setBusy(true);
    try {
      setPreview(await post<DecisionPreview>("/verification/budget/preview", { budget: wanted() }));
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    if (!preview) return;
    setError(null);
    setBusy(true);
    try {
      await post("/verification/budget", { budget: wanted(), digest: preview.digest });
      setPreview(null);
      announce("Verification budget changed.");
      onApplied();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const edited = () => setPreview(null);
  return (
    <form className="budget-form" onSubmit={(e) => void review(e)} data-testid="budget-form">
      <div className="form-row">
        <label htmlFor="budget-each">Minutes per check</label>
        <input
          id="budget-each"
          type="number"
          min={1}
          max={1440}
          value={each}
          onChange={(e) => {
            setEach(e.target.value);
            edited();
          }}
        />
      </div>
      <div className="form-row">
        <label htmlFor="budget-day">Minutes per day</label>
        <input
          id="budget-day"
          type="number"
          min={1}
          max={1440}
          value={day}
          onChange={(e) => {
            setDay(e.target.value);
            edited();
          }}
        />
      </div>
      <div className="form-row">
        <label htmlFor="budget-root">Output folder: research folder</label>
        <select
          id="budget-root"
          value={alias}
          onChange={(e) => {
            setAlias(e.target.value);
            edited();
          }}
        >
          <option value="">No output folder</option>
          {aliases.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="budget-path">Folder inside it</label>
        <input
          id="budget-path"
          value={path}
          disabled={!alias}
          placeholder="checks"
          onChange={(e) => {
            setPath(e.target.value);
            edited();
          }}
        />
      </div>
      {preview ? (
        <div className="stage-confirm" data-testid="budget-preview">
          {preview.would_apply ? (
            <>
              <p className="lead-label">What changes (recorded by PrometheOS)</p>
              <ul data-testid="budget-effects">
                {preview.protected_effects.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
              <button
                type="button"
                className="button primary small"
                disabled={busy}
                onClick={() => void apply()}
                data-testid="budget-apply"
              >
                Apply this budget
              </button>{" "}
              <button type="button" className="button secondary small" onClick={() => setPreview(null)}>
                Cancel
              </button>
            </>
          ) : (
            <p className="inline-warning" role="alert" data-testid="budget-refused">
              This budget would not apply as it stands (an output folder must be inside an approved research
              folder); nothing was changed.
            </p>
          )}
        </div>
      ) : (
        <button type="submit" className="button secondary small" disabled={busy} data-testid="budget-review">
          Review the change
        </button>
      )}
      {error ? <ErrorState error={error} /> : null}
    </form>
  );
}

function OpenChecks({ checks, line }: { checks: PlannedCheck[]; line: string }) {
  const resolved = checks.filter((c) => c.resolved);
  const open = checks.filter((c) => !c.resolved);
  return (
    <>
      {resolved.length ? (
        <div data-testid="checks-resolved">
          <p className="lead-label">Their cause is gone</p>
          <CheckList checks={resolved} />
        </div>
      ) : null}
      <p data-testid="checks-summary">{line}</p>
      <CheckList checks={open.slice(0, 5)} />
      {open.length > 5 ? (
        <Details label={`All ${open.length} checks`}>
          <CheckList checks={open.slice(5)} />
        </Details>
      ) : null}
    </>
  );
}

export function PlannedChecks() {
  const planned = useQuery<VerificationPlanView>("/verification/plan");
  const [error, setError] = useState<unknown>(null);
  const [note, setNote] = useState<string | null>(null);
  const propose = async () => {
    setError(null);
    try {
      const made = await post<{ created: string[] }>("/verification/propose", {});
      setNote(made.created.length ? `${made.created.length} new check(s) planned.` : "No new check was needed.");
      planned.reload();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section className="section checks" aria-labelledby="checks-title" data-testid="planned-checks">
      <h2 id="checks-title">Checks planned</h2>
      <QueryView query={planned} loadingLabel="Loading planned checks">
        {(data) => (
          <>
            <p className="muted" data-testid="budget">
              Your agent may start read-only checks up to {data.budget.max_minutes_each} min each and{" "}
              {data.budget.max_minutes_per_day} min a day ({data.budget.used_today} used today).
              {data.budget.output_path
                ? ` New files go to ${data.budget.output_root_alias}/${data.budget.output_path}.`
                : " No output folder is set yet."}
            </p>
            <Details label="Change the budget">
              <BudgetForm
                key={`${data.budget.max_minutes_each}-${data.budget.max_minutes_per_day}-${data.budget.output_path ?? ""}`}
                budget={data.budget}
                onApplied={planned.reload}
              />
            </Details>
            {data.checks.length === 0 ? (
              <Empty>No check is planned.</Empty>
            ) : (
              <OpenChecks checks={data.checks} line={summary(data)} />
            )}
          </>
        )}
      </QueryView>
      <button type="button" className="button secondary small" data-testid="plan-checks" onClick={() => void propose()}>
        Plan checks for uncertain claims
      </button>
      {note ? (
        <p className="info" role="status">
          {note}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </section>
  );
}

