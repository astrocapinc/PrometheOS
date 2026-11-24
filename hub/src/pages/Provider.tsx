// AI provider: where (if anywhere) model calls may go, what a request would send, and what it
// may cost. Optional by design: with the default policy (no AI) nothing here is needed and the
// rest of the hub is unaffected. The hub never takes or shows a credential value.
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, describeError, post, runOperation } from "../api";
import { LayerSection } from "../components/LayerSection";
import { CredentialDelete } from "../components/parity";
import { Details, ErrorState, KeyValues, PageHeading, QueryView, formatDate } from "../components/ui";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { ProviderCall, ProviderConfigPreview, ProviderPreview, ProviderStatus } from "../types";

const POLICY_TEXT: Record<string, string> = {
  "no-ai":
    "AI is off for this project. Capture, notebook, search, corrections and tasks all work without it, and your coding agent can still do AI work through its own provider.",
  "local-only": "Model calls may go only to a server on this computer. Nothing is sent over the network.",
  "remote-allowed":
    "Model calls may go to the configured service. Each request is previewed first and needs your approval; restricted records are never included unless you confirm that separately.",
};

const POLICY_NAME: Record<string, string> = {
  "no-ai": "Off (no AI)",
  "local-only": "Local only",
  "remote-allowed": "Remote allowed",
};

export function ProviderPage() {
  const status = useQuery<ProviderStatus>("/provider");
  const calls = useQuery<{ calls: ProviderCall[] }>("/provider/calls");
  return (
    <>
      <PageHeading
        title="AI provider"
        lead="Optional. Choose where model calls may go, see what a request would send, and keep spending bounded."
      />
      <QueryView query={status} loadingLabel="Loading provider settings">
        {(s) => (
          <>
            <section aria-labelledby="route-title" className="audio-section" data-testid="provider-summary">
              <h2 id="route-title">Where AI runs</h2>
              <p data-testid="provider-policy">
                <strong>{POLICY_NAME[s.policy] ?? s.policy}</strong>
                {s.route !== "none" ? (
                  <>
                    {" "}
                    · route <strong data-testid="provider-route">{s.route}</strong> · <code>{s.endpoint_host}</code>
                  </>
                ) : null}
              </p>
              <p>{POLICY_TEXT[s.policy]}</p>
              <p className="muted">There is no automatic switch between routes: if one fails, nothing is sent another way.</p>
              <table className="plain-table" data-testid="capability-table">
                <caption className="visually-hidden">What each kind of AI work uses</caption>
                <thead>
                  <tr>
                    <th scope="col">Kind of work</th>
                    <th scope="col">Provided by</th>
                    <th scope="col">Route</th>
                    <th scope="col">Available</th>
                  </tr>
                </thead>
                <tbody>
                  {s.capabilities.map((c) => (
                    <tr key={c.capability}>
                      <th scope="row">{c.capability}</th>
                      <td>{c.provider ?? "none yet"}</td>
                      <td>{c.route}</td>
                      <td>{c.available ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
            <section aria-labelledby="cred-title" className="audio-section">
              <h2 id="cred-title">Credential</h2>
              {s.credential.reference ? (
                <p data-testid="credential-health">
                  Reference <code>{s.credential.reference}</code>:{" "}
                  <strong>{s.credential.present ? "a value is stored" : "no value stored"}</strong> ({s.credential.store}). The value is never shown here.
                </p>
              ) : (
                <p className="muted">No credential is configured (a local server needs none).</p>
              )}
              <Details label="Manage the credential">
                <p className="muted small-print">
                  Enter the credential through the terminal's hidden prompt:{" "}
                  <code>prometheos provider secret set --reference {s.credential.reference ?? "NAME"}</code>
                </p>
                <CredentialDelete reference={s.credential.reference} onDone={status.reload} />
              </Details>
            </section>
            <section aria-labelledby="usage-title" className="audio-section">
              <h2 id="usage-title">Spending</h2>
              <KeyValues
                rows={[
                  ["Calls so far", String(s.usage.calls)],
                  ["Estimated spend", `${s.usage.spent_estimate_usd} USD`],
                  ["Cap", s.usage.cap_usd ? `${s.usage.cap_usd} USD` : "none set"],
                  ["Unknown outcomes", String(s.usage.unknown_outcomes)],
                ]}
              />
              <p className="muted small-print">{s.usage.label}</p>
            </section>
            <ConfigForm status={s} />
          </>
        )}
      </QueryView>
      <LayerSection />
      <section aria-labelledby="ledger-title" className="audio-section">
        <h2 id="ledger-title">Calls made</h2>
        <QueryView query={calls} isEmpty={(d) => d.calls.length === 0} empty={<>No model call has been made.</>}>
          {(d) => (
            <table className="plain-table" data-testid="provider-calls">
              <caption className="visually-hidden">Provider calls: when, route, state and cost</caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Purpose</th>
                  <th scope="col">Route</th>
                  <th scope="col">State</th>
                  <th scope="col">Cost (estimate)</th>
                </tr>
              </thead>
              <tbody>
                {d.calls.map((c) => (
                  <tr key={c.call_id}>
                    <td>{formatDate(c.created_at)}</td>
                    <td>{c.purpose.replace(/_/g, " ")}</td>
                    <td>
                      {c.route} · {c.endpoint_host}
                    </td>
                    <td>{c.state}</td>
                    <td>{c.actual_cost_usd ?? c.reserved_cost_usd ?? "n/a"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryView>
      </section>
    </>
  );
}

function ConfigForm({ status }: { status: ProviderStatus }) {
  const { announce, refresh } = useHub();
  const c = status.config;
  const [policy, setPolicy] = useState(c.policy);
  const [endpoint, setEndpoint] = useState(c.endpoint ?? "");
  const [model, setModel] = useState(c.model ?? "");
  const [ref, setRef] = useState(c.credential_ref ?? "");
  const [priceIn, setPriceIn] = useState(c.price_input_per_mtok_usd ?? "");
  const [priceOut, setPriceOut] = useState(c.price_output_per_mtok_usd ?? "");
  const [cap, setCap] = useState(c.budget.spend_cap_usd ?? "");
  const [maxChars, setMaxChars] = useState(String(c.budget.max_input_chars));
  const [maxOut, setMaxOut] = useState(String(c.budget.max_output_tokens));
  const [calls, setCalls] = useState(String(c.budget.max_calls_per_run));
  const [timeout, setTimeoutSeconds] = useState(String(c.budget.timeout_seconds));
  const [review, setReview] = useState<ProviderConfigPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [applied, setApplied] = useState(false);

  useEffect(() => setReview(null), [policy, endpoint, model, ref, priceIn, priceOut, cap, maxChars, maxOut, calls, timeout]);

  const build = () => ({
    policy,
    endpoint: endpoint.trim() || null,
    model: model.trim() || null,
    credential_ref: ref.trim() || null,
    price_input_per_mtok_usd: priceIn.trim() || null,
    price_output_per_mtok_usd: priceOut.trim() || null,
    budget: {
      max_input_chars: Number(maxChars),
      max_output_tokens: Number(maxOut),
      max_calls_per_run: Number(calls),
      spend_cap_usd: cap.trim() || null,
      timeout_seconds: Number(timeout),
    },
  });

  const preview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setApplied(false);
    try {
      setReview(await post<ProviderConfigPreview>("/provider/configure/preview", { config: build() }));
    } catch (err) {
      setError(err);
      setReview(null);
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!review) return;
    setBusy(true);
    setError(null);
    try {
      await post("/provider/configure", { config: build(), digest: review.digest });
      announce("AI provider settings saved.");
      setApplied(true);
      setReview(null);
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="cfg-title" className="audio-section">
      <h2 id="cfg-title">Settings</h2>
      <form className="capture-form" onSubmit={preview} aria-labelledby="cfg-title" noValidate>
        <div className="field">
          <label htmlFor="cfg-policy">Where model calls may go</label>
          <select id="cfg-policy" value={policy} onChange={(e) => setPolicy(e.target.value)}>
            <option value="no-ai">Off: no AI (default)</option>
            <option value="local-only">Local only: a server on this computer</option>
            <option value="remote-allowed">Remote allowed: a service I name below</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="cfg-endpoint">Endpoint</label>
          <input id="cfg-endpoint" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://api.example.com/v1" />
          <p className="muted small-print">Chat Completions style, JSON-schema output. Remote services need https.</p>
        </div>
        <div className="field">
          <label htmlFor="cfg-model">Model</label>
          <input id="cfg-model" value={model} onChange={(e) => setModel(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="cfg-ref">Credential reference (a name, not the key)</label>
          <input id="cfg-ref" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="provider-key" />
        </div>
        <div className="field">
          <label htmlFor="cfg-pin">Price per million input tokens (USD)</label>
          <input id="cfg-pin" inputMode="decimal" value={priceIn} onChange={(e) => setPriceIn(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="cfg-pout">Price per million output tokens (USD)</label>
          <input id="cfg-pout" inputMode="decimal" value={priceOut} onChange={(e) => setPriceOut(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="cfg-cap">Spending cap (USD, optional)</label>
          <input id="cfg-cap" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} />
          <p className="muted small-print">Needs both prices. Prices are yours; none are built in.</p>
        </div>
        <div className="field">
          <label htmlFor="cfg-chars">Largest request (characters)</label>
          <input id="cfg-chars" inputMode="numeric" value={maxChars} onChange={(e) => setMaxChars(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="cfg-out">Largest reply (tokens)</label>
          <input id="cfg-out" inputMode="numeric" value={maxOut} onChange={(e) => setMaxOut(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="cfg-calls">Calls per request</label>
          <select id="cfg-calls" value={calls} onChange={(e) => setCalls(e.target.value)}>
            <option value="1">1 (no repair attempt)</option>
            <option value="2">2 (one repair attempt at most)</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="cfg-timeout">Wait at most (seconds)</label>
          <input id="cfg-timeout" inputMode="numeric" value={timeout} onChange={(e) => setTimeoutSeconds(e.target.value)} />
        </div>
        {error ? <ErrorState error={error} /> : null}
        {applied ? (
          <p className="success" role="status" data-testid="config-applied">
            <span aria-hidden="true">✓ </span>Settings saved.
          </p>
        ) : null}
        <button type="submit" className="button primary" disabled={busy} aria-busy={busy}>
          Review the change
        </button>
      </form>
      {review ? (
        <section className="decision" aria-labelledby="cfg-review" data-testid="config-review" tabIndex={-1}>
          <h3 id="cfg-review">Review before saving</h3>
          <p>
            Policy <strong>{POLICY_NAME[review.policy] ?? review.policy}</strong>, route <strong>{review.route}</strong>. Changes:{" "}
            {review.changes.length ? review.changes.join(", ") : "none"}.
          </p>
          {review.credential.reference && !review.credential.present ? (
            <p className="inline-warning">No credential value is stored yet for “{review.credential.reference}”.</p>
          ) : null}
          <Details>
            <p>
              Digest: <code>{review.digest}</code>
            </p>
          </Details>
          <div className="button-row">
            <button type="button" className="button primary" onClick={() => void apply()} disabled={busy || review.stale}>
              Save these settings
            </button>
            <button type="button" className="button secondary" onClick={() => setReview(null)}>
              Cancel
            </button>
          </div>
        </section>
      ) : null}
    </section>
  );
}

// ----- one bounded request from a meeting -----------------------------------------------------

export function ProviderRunPanel({ meetingId, onDone }: { meetingId: string; onDone: () => void }) {
  const { announce, refresh } = useHub();
  const status = useQuery<ProviderStatus>("/provider");
  const [shown, setShown] = useState<ProviderPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const body = { purpose: "meeting_extract", meeting_id: meetingId };
  const preview = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setShown(await post<ProviderPreview>("/provider/preview", body));
      setOpen(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const run = async (acknowledge?: string) => {
    if (!shown) return;
    setBusy(true);
    setError(null);
    try {
      await runOperation("/provider/run", { ...body, digest: shown.digest, acknowledge_unknown: acknowledge ?? null });
      setResult("The provider's proposals were added as items below. Nothing is adopted until you approve it.");
      announce("Proposed items added from the provider.");
      setShown(null);
      setOpen(false);
      void refresh().catch(() => undefined);
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const s = status.data;
  const off = !s || s.policy === "no-ai";
  const failure = error instanceof ApiError ? error : null;
  const details = failure?.details as { status?: string; task_id?: string; calls?: string[] } | undefined;
  return (
    <section aria-labelledby="prov-title" className="provider-run" data-testid="provider-run">
      <h2 id="prov-title">Propose items with AI (optional)</h2>
      {off ? (
        <p className="muted" data-testid="provider-off">
          AI is off for this project, and nothing here needs it. Your coding agent can propose items from these notes, or you can add them yourself.{" "}
          <Link to="/ai">AI provider settings</Link>
        </p>
      ) : (
        <>
          <p className="muted">
            Route: {s?.route} ({s?.endpoint_host}). You see exactly what would be sent before anything is.
          </p>
          <button type="button" className="button secondary" onClick={() => void preview()} disabled={busy} aria-busy={busy}>
            Preview what would be sent
          </button>
        </>
      )}
      {open && shown ? (
        <div className="decision" data-testid="provider-preview" role="group" aria-label="Preview of the request">
          <p>
            <strong>{shown.would_send ? "Ready to send" : "Cannot send"}</strong> to <code>{shown.endpoint_host}</code> ({shown.route}) with model{" "}
            <code>{shown.model}</code>.
          </p>
          <KeyValues
            rows={[
              ["Characters sent", String(shown.transmits.characters)],
              ["Estimated cost (upper bound)", shown.cost.estimate_upper_bound_usd ? `${shown.cost.estimate_upper_bound_usd} USD` : "unknown (no prices set)"],
              ["Records included", `${shown.inputs.records} (${shown.inputs.sensitivity})`],
              ["Restricted records", shown.inputs.restricted_included
                ? "included"
                : shown.inputs.excluded_above_scope === null
                  ? "not included"
                  : `not included (${shown.inputs.excluded_above_scope} left out)`],
              ["Calls", `up to ${shown.calls.max_per_run} (one repair at most)`],
            ]}
          />
          <p className="muted small-print">{shown.cost.label}</p>
          {shown.blockers.length > 0 ? (
            <ul className="effects" data-testid="provider-blockers">
              {shown.blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          ) : null}
          <Details label="Exactly what would be sent (start of the text)">
            <pre className="command" data-testid="provider-excerpt">
              {shown.transmits.excerpt}
              {shown.transmits.excerpt_truncated ? "\n…" : ""}
            </pre>
          </Details>
          <div className="button-row">
            <button type="button" className="button primary" disabled={busy || !shown.would_send} onClick={() => void run()}>
              Send this request
            </button>
            <button type="button" className="button secondary" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <div className="state state-error" role="alert" data-testid="provider-failure">
          <p className="state-title">{details?.status === "unknown_outcome" ? "The provider did not answer in time" : describeError(error).title}</p>
          <p>{failure?.message ?? describeError(error).detail}</p>
          {details?.task_id ? (
            <p data-testid="provider-fallback">
              Nothing else changed. The same task stays open for your coding agent (task <code>{details.task_id}</code>); it does not depend on this provider.
            </p>
          ) : null}
          {details?.status === "unknown_outcome" && details.calls?.[0] ? (
            <p className="muted">
              The request may have been received and billed. It is not repeated automatically; sending again is your choice.{" "}
              <button type="button" className="link-button" onClick={() => void preview()}>
                Preview again
              </button>
            </p>
          ) : null}
        </div>
      ) : null}
      {result ? (
        <p className="success" role="status" data-testid="provider-result">
          <span aria-hidden="true">✓ </span>
          {result}
        </p>
      ) : null}
      {shown && shown.unacknowledged_unknown_calls.length > 0 ? (
        <p className="inline-warning">
          An earlier call for this request had an unknown outcome and may have been billed.{" "}
          <button
            type="button"
            className="button secondary small"
            disabled={busy}
            onClick={() => void run(shown.unacknowledged_unknown_calls[0])}
          >
            I understand; send again
          </button>
        </p>
      ) : null}
    </section>
  );
}

