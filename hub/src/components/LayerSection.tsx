// The intelligence layer (A07, CP22.4; decision 0094): the owner's standing policy, the key's
// health, this month's spend across the home, each research root's sensitivity, and what the
// layer proposed. Everything works without it: with no policy in force, each task runs its rules
// or the coding agent's path and says so. The hub never takes or shows a key; the policy is
// granted from the terminal, where the owner confirms it.
import { useState } from "react";
import { describeError, post } from "../api";
import { Details, KeyValues, QueryView, formatDate } from "./ui";
import { useQuery } from "../hub";
import type { AiEvaluations, AiPolicyState, AiResults, RootSensitivities } from "../types";

export const SOURCE: Record<string, string> = {
  gateway: "model (via the provider)",
  cassette: "recorded reply (no call, no trust)",
  agent: "your coding agent",
  rules: "rules",
};

export function LayerSection() {
  const policy = useQuery<AiPolicyState>("/ai/policy");
  const roots = useQuery<RootSensitivities>("/ai/root-sensitivity");
  const results = useQuery<AiResults>("/ai/results?limit=20");
  const evaluations = useQuery<AiEvaluations>("/ai/evaluations");
  return (
    <section aria-labelledby="layer-title" className="audio-section" data-testid="ai-layer">
      <h2 id="layer-title">Intelligence layer</h2>
      <QueryView query={policy} loadingLabel="Loading the standing policy">
        {(p) => (
          <>
            {p.in_force && p.policy ? (
              <p data-testid="ai-policy-in-force">
                <strong>On.</strong> Small typed tasks may use the provider within your standing policy
                (revision {p.policy.revision}, granted {formatDate(p.policy.granted_at)}). Every proposal is
                checked by rules before it is shown, and none changes a status, class or decision by itself.
              </p>
            ) : (
              <p data-testid="ai-policy-off">
                <strong>Off.</strong>{" "}
                {p.void_reason
                  ? `The policy no longer applies: ${p.void_reason}. Grant it again to turn the layer back on.`
                  : "No standing policy is granted."}{" "}
                Every task still runs its rules, or your coding agent does the reading, and says what it did instead.
              </p>
            )}
            <KeyValues
              rows={[
                ["Key", p.key.reference ? `${p.key.reference}: ${p.key.present ? "a value is stored" : "no value stored"}` : "no reference configured"],
                ["This month (all projects)", `${p.spend.used} USD of ${p.spend.monthly_cap ?? "no cap set"}`],
                ["Billed / estimated", `${p.spend.billed} / ${p.spend.estimated} USD`],
                ["Calls with an unknown outcome", String(p.spend.held_calls)],
                ["Registered tasks", p.tasks.join(", ")],
              ]}
            />
            <Details label="Grant or change the policy">
              <p className="muted small-print">
                Write the policy (task kinds, evaluated models, caps, whether agents may start runs) to a file and run{" "}
                <code>prometheos ai policy grant --file policy.json</code> in a terminal; it shows exactly what it binds
                before you confirm. Enter the key with <code>prometheos provider secret set</code>, never here.
              </p>
            </Details>
            {p.in_force ? <RevokePolicy onDone={policy.reload} /> : null}
          </>
        )}
      </QueryView>
      <h3>Research roots</h3>
      <QueryView query={roots} isEmpty={(r) => r.roots.length === 0} empty={<>No research root is approved.</>}>
        {(r) => (
          <table className="plain-table" data-testid="ai-root-sensitivity">
            <caption className="visually-hidden">The sensitivity of each research root</caption>
            <thead>
              <tr>
                <th scope="col">Root</th>
                <th scope="col">Sensitivity</th>
                <th scope="col">Note</th>
              </tr>
            </thead>
            <tbody>
              {r.roots.map((root) => (
                <tr key={root.root_alias}>
                  <th scope="row">{root.root_alias}</th>
                  <td>{root.sensitivity}</td>
                  <td>{root.note ?? "set by you"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryView>
      <p className="muted small-print">
        Only files under a root you set to normal can be sent. Lower a root with{" "}
        <code>prometheos adopt sensitivity set ALIAS normal</code> (you confirm it in the terminal).
      </p>
      <h3>What the layer proposed</h3>
      <QueryView query={results} isEmpty={(r) => r.results.length === 0} empty={<>No task has run yet.</>}>
        {(r) => (
          <table className="plain-table" data-testid="ai-results">
            <caption className="visually-hidden">Recent task samples with their source</caption>
            <thead>
              <tr>
                <th scope="col">Task</th>
                <th scope="col">Subject</th>
                <th scope="col">What it says</th>
                <th scope="col">Outcome</th>
                <th scope="col">Source</th>
                <th scope="col">
                  <span className="visually-hidden">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {r.results.map((row) => (
                <tr key={row.result_key}>
                  <td>{row.task_kind}</td>
                  <td>
                    <code>{row.subject}</code>
                  </td>
                  <td data-testid="ai-result-summary">{row.summary ?? "—"}</td>
                  <td>
                    {row.outcome}
                    {row.reasons.length ? ` (${row.reasons.join(", ")})` : ""}
                    {row.hint_only ? " · a hint" : ""}
                  </td>
                  <td data-testid="ai-result-source">{SOURCE[row.executor] ?? row.executor}</td>
                  <td>
                    {row.outcome === "accepted" && row.scan_gen !== null && row.sample === 0 ? (
                      <AcceptProposal row={row} onDone={results.reload} />
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryView>
      <h3>Measured trust</h3>
      <QueryView
        query={evaluations}
        isEmpty={(e) => e.evaluations.length === 0}
        empty={<>No task has been measured, so every proposal is shown as a hint.</>}
      >
        {(e) => (
          <ul data-testid="ai-evaluations">
            {e.evaluations.map((row) => (
              <li key={row.evaluation_id} data-testid="ai-evaluation" data-trusted={row.trusted ? "yes" : "no"}>
                {row.task_kind} v{row.task_version} on {row.model.split("+").join(" and ") || "several models"}:{" "}
                <strong>{row.trusted ? "trusted" : "not trusted"}</strong> ({row.correct} of {row.answered} answered right,
                coverage {Math.round(row.coverage * 100)}%, precision at least{" "}
                {Math.round(row.precision_lower_bound * 100)}% at 95%, {row.executor})
                {row.why ? (
                  <span className="muted small-print" data-testid="ai-evaluation-why">
                    {" "}
                    Why not: {row.why}.
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </QueryView>
    </section>
  );
}

function RevokePolicy({ onDone }: { onDone: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const revoke = async () => {
    try {
      await post("/ai/policy/revoke", {});
      setConfirming(false);
      onDone();
    } catch (err) {
      const described = describeError(err);
      setError(`${described.title}: ${described.detail}`);
    }
  };
  return (
    <div className="decision" data-testid="ai-policy-revoke">
      {confirming ? (
        <>
          <p>Stop the layer's remote calls now? Tasks keep running their rules and your agent's path.</p>
          <button type="button" className="button danger" onClick={() => void revoke()}>
            Revoke the policy
          </button>{" "}
          <button type="button" className="button secondary" onClick={() => setConfirming(false)}>
            Keep it
          </button>
        </>
      ) : (
        <button type="button" className="button secondary" onClick={() => setConfirming(true)}>
          Revoke the standing policy…
        </button>
      )}
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}

type ResultRow = AiResults["results"][number];

function AcceptProposal({ row, onDone }: { row: ResultRow; onDone: () => void }) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  const accept = async () => {
    setState("busy");
    try {
      // This row's sample; the service finds its sample set (review cp22-a R3-F3, R5-F2).
      await post("/ai/accept", {
        task_kind: row.task_kind,
        scan_gen: row.scan_gen,
        subject: row.subject,
        result_keys: [row.result_key],
      });
      setState("done");
      onDone();
    } catch (err) {
      const described = describeError(err);
      setError(`${described.title}: ${described.detail}`);
      setState("idle");
    }
  };
  if (state === "done") return <span data-testid="ai-accepted">used as evidence</span>;
  return (
    <>
      <button
        type="button"
        className="button secondary small"
        data-testid="ai-accept"
        aria-busy={state === "busy"}
        onClick={() => void accept()}
      >
        Use as evidence
      </button>
      {error ? <span role="alert">{error}</span> : null}
    </>
  );
}
