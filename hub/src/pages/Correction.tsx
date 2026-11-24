// Correction review. "Proposed" and "Applied" look and behave differently:
// proposed shows old claim vs new evidence and the affected records, then the two-step
// owner decision; applied shows the committed revision, the claim's new state, follow-up
// work and what the next agent context says. Nothing is shown as applied before commit.
import { useState } from "react";
import { DecisionReview } from "../components/DecisionReview";
import {
  Details,
  ErrorState,
  KeyValues,
  Loading,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  lineLabel,
} from "../components/ui";
import { useKindList, useRecords } from "../data";
import { useQuery } from "../hub";
import { Link } from "../router";
import { ScopeList } from "./Knowledge";
import type {
  ClaimBody,
  ContextPack,
  CorrectionBody,
  DecisionApplied,
  Envelope,
  EvidenceRef,
  ProjectInfo,
  SourceBody,
  VersionText,
} from "../types";
import { get, uuid4 } from "../api";

export function CorrectionPage({ id }: { id: string }) {
  const correction = useQuery<Envelope<CorrectionBody>>(`/records/${encodeURIComponent(id)}`);
  const [applied, setApplied] = useState<DecisionApplied | null>(null);
  return (
    <>
      <PageHeading title="Correction" />
      <QueryView query={correction} loadingLabel="Loading correction">
        {(c) => <CorrectionView correction={c} applied={applied} onApplied={setApplied} />}
      </QueryView>
    </>
  );
}

function CorrectionView({
  correction,
  applied,
  onApplied,
}: {
  correction: Envelope<CorrectionBody>;
  applied: DecisionApplied | null;
  onApplied: (a: DecisionApplied) => void;
}) {
  const body = correction.body;
  const ids = [...body.target_refs.map((r) => r.id), ...body.replacement_refs.map((r) => r.id)];
  const records = useRecords(ids);
  const sources = useKindList<SourceBody>("source");
  const sourceTitle = (sid: string) =>
    sources.data?.items.find((s) => s.id === sid)?.body.title ?? "Source";
  const isApplied = body.status === "applied";

  const build = async () => {
    const [fresh, project] = await Promise.all([
      get<Envelope<CorrectionBody>>(`/records/${encodeURIComponent(correction.id)}`),
      get<ProjectInfo>("/project"),
    ]);
    return {
      schema_version: 1,
      project_id: project.project_id,
      request_id: uuid4(),
      expected_seq: project.project_seq,
      operations: [{ op: "correction_apply", id: fresh.id, base_rev: fresh.rev }],
    };
  };

  // A target is a claim, or (from CP10) an assumption entry; only a claim has a statement,
  // scope and state (S11.4b: an assumption's correction rendered a blank page before).
  const claim = (rid: string) => {
    const found = records.records.get(rid);
    return found && found.kind === "claim" ? (found as unknown as Envelope<ClaimBody>) : undefined;
  };
  const named = (rid: string) => {
    const found = records.records.get(rid) as Envelope<{ title?: string; text?: string }> | undefined;
    return found && found.kind !== "claim" ? found : undefined;
  };

  return (
    <article
      className={`correction correction-${body.status}`}
      data-testid="correction"
      data-status={body.status}
      aria-labelledby="correction-status"
    >
      <div className={`status-banner banner-${body.status}`} id="correction-status">
        {isApplied ? (
          <>
            <span aria-hidden="true">✓ </span>
            <strong>Applied correction</strong> — committed at project sequence {body.applied_seq}.
            The records below show their committed state.
          </>
        ) : body.status === "proposed" ? (
          <>
            <span aria-hidden="true">◇ </span>
            <strong>Proposed correction</strong> — not applied. Nothing changes until you approve
            it.
          </>
        ) : (
          <>
            <strong>Rejected correction</strong> — it was not applied.
          </>
        )}
      </div>
      <p className="reason">
        <strong>Reason:</strong> {body.reason}
      </p>

      <section aria-labelledby="compare-title" className="section">
        <h2 id="compare-title">{isApplied ? "What changed" : "Compare"}</h2>
        {records.loading && records.records.size === 0 ? <Loading label="Loading claims" /> : null}
        {records.error ? <ErrorState error={records.error} /> : null}
        {body.effects.map((effect, i) => {
          const target = claim(effect.target_ref.id);
          const replacement = effect.replacement_ref ? claim(effect.replacement_ref.id) : undefined;
          return (
            <div key={i} className="compare">
              <div className="compare-col old" data-testid="old-claim">
                <h3>{isApplied ? "Superseded claim" : "Current claim (would be superseded)"}</h3>
                {target ? (
                  <>
                    <p className="statement">{target.body.statement}</p>
                    <p>
                      State now: <StateBadge state={target.body.state} prefix="Claim state" />
                    </p>
                    <ScopeList scope={target.body.scope} />
                  </>
                ) : named(effect.target_ref.id) ? (
                  <p className="statement" data-testid="old-entry">
                    {named(effect.target_ref.id)?.body.title}: {named(effect.target_ref.id)?.body.text}
                  </p>
                ) : (
                  <p className="muted">Claim not available.</p>
                )}
              </div>
              <div className="compare-col new" data-testid="replacement-claim">
                <h3>{effect.effect === "supersede" ? "Replacement claim" : "Effect"}</h3>
                {replacement ? (
                  <>
                    <p className="statement">{replacement.body.statement}</p>
                    <p>
                      State: <StateBadge state={replacement.body.state} prefix="Claim state" />
                    </p>
                    <ScopeList scope={replacement.body.scope} />
                  </>
                ) : effect.replacement_ref && named(effect.replacement_ref.id) ? (
                  <p className="statement" data-testid="replacement-entry">
                    {named(effect.replacement_ref.id)?.body.title}: {named(effect.replacement_ref.id)?.body.text}
                  </p>
                ) : (
                  <p>{effect.replacement_text ?? effect.effect.replace(/_/g, " ")}</p>
                )}
              </div>
            </div>
          );
        })}
      </section>

      <section aria-labelledby="evidence-title" className="section">
        <h2 id="evidence-title">New evidence</h2>
        {body.evidence.length === 0 ? <p className="muted">No evidence cited.</p> : null}
        {body.evidence.map((ev, i) => (
          <EvidenceExcerpt key={i} evidence={ev} title={sourceTitle(ev.source_id)} citedBy={body.reason} />
        ))}
      </section>

      {body.status === "proposed" && applied ? (
        <Loading label="Committed. Loading the applied correction" />
      ) : null}
      {body.status === "proposed" && !applied ? (
        <section aria-labelledby="decide-title" className="section">
          <h2 id="decide-title">Decide</h2>
          <DecisionReview
            startLabel="Preview applying this correction"
            title="apply this correction"
            approveLabel="Approve correction"
            affected={
              <ul data-testid="affected-records">
                {body.effects.map((e, i) => (
                  <li key={i}>
                    {claim(e.target_ref.id) ? "Claim" : "Record"} “
                    {claim(e.target_ref.id)?.body.statement ?? named(e.target_ref.id)?.body.title ?? e.target_ref.id}” will be{" "}
                    <strong>
                      {e.effect === "supersede" ? "marked Superseded" : e.effect.replace(/_/g, " ")}
                    </strong>
                    {e.replacement_ref ? (
                      <>
                        , replaced by “{claim(e.replacement_ref.id)?.body.statement ?? named(e.replacement_ref.id)?.body.title ?? e.replacement_ref.id}”
                      </>
                    ) : null}
                    .
                  </li>
                ))}
                <li>Agent context will list the superseded claim as historical.</li>
              </ul>
            }
            build={build}
            onApplied={onApplied}
          />
        </section>
      ) : null}

      {isApplied ? <AppliedResult applied={applied} /> : null}

      <Details>
        <KeyValues
          rows={[
            ["Correction", <code key="c">{correction.id}</code>],
            ["Revision", String(correction.rev)],
            ["Status", body.status],
            ["Applied at sequence", body.applied_seq === null ? "not applied" : String(body.applied_seq)],
          ]}
        />
      </Details>
    </article>
  );
}

function EvidenceExcerpt({
  evidence,
  title,
  citedBy,
}: {
  evidence: EvidenceRef;
  title: string;
  citedBy: string;
}) {
  const text = useQuery<VersionText>(`/versions/${encodeURIComponent(evidence.version_id)}/text`);
  const start = evidence.locator.start_line;
  const end = evidence.locator.end_line ?? start;
  let excerpt: string | null = null;
  if (text.data?.text != null && start !== undefined) {
    excerpt = text.data.text.split("\n").slice(start - 1, (end ?? start)).join("\n");
  }
  return (
    <figure className="evidence" data-testid="evidence-excerpt">
      {text.data === undefined && text.loading ? <Loading label="Loading evidence" /> : null}
      {text.error ? <ErrorState error={text.error} onRetry={text.reload} /> : null}
      {text.data && text.data.text == null ? (
        <p className="state state-unavailable">
          Evidence text unavailable. Reason: {text.data.reason ?? "not stated"}.
        </p>
      ) : null}
      {excerpt !== null ? <blockquote className="excerpt-quote">{excerpt}</blockquote> : null}
      <figcaption>
        {title}, {lineLabel(start, end)} (exact retained text) ·{" "}
        <SourceButton
          spec={{ versionId: evidence.version_id, title, startLine: start, endLine: end, citedBy }}
        >
          Open in context
        </SourceButton>
      </figcaption>
    </figure>
  );
}

function AppliedResult({ applied }: { applied: DecisionApplied | null }) {
  const context = useQuery<ContextPack>("/context?purpose=orient&budget=12000");
  const sections = context.data ? pickSections(context.data.text, ["corrections", "needs_review"]) : null;
  return (
    <section aria-labelledby="result-title" className="section" data-testid="applied-result">
      <h2 id="result-title">Result</h2>
      {applied ? (
        <p className="success" role="status">
          <span aria-hidden="true">✓ </span>Committed at project sequence {applied.result.project_seq}
          {applied.result.replayed ? " (already applied earlier; replayed)" : ""}.
        </p>
      ) : null}
      <h3>Follow-up and what your coding agent sees next</h3>
      {context.data === undefined && context.loading ? <Loading label="Loading next context" /> : null}
      {context.error ? <ErrorState error={context.error} onRetry={context.reload} /> : null}
      {context.data ? (
        <>
          <ul data-testid="next-context-summary">
            <li>
              {context.data.text.includes("APPLIED CORRECTION")
                ? "The next context your coding agent receives lists this correction as applied."
                : "The next context does not mention this correction yet."}
            </li>
            {context.data.text.includes("HISTORICAL (superseded") ? (
              <li>The replaced claim appears only as historical: “do not rely on it”.</li>
            ) : null}
            <li>
              Follow-up needing review:{" "}
              {(context.data.manifest.sections.find((x) => x.name === "needs_review")?.included.length ?? 0) || "none"}
            </li>
          </ul>
          <Details label="Exact context text">
            <pre className="context-text" data-testid="next-context">
              {sections || "No correction or follow-up sections in the next context."}
            </pre>
          </Details>
        </>
      ) : null}
      <p>
        <Link to="/knowledge">Back to Knowledge</Link>
      </p>
    </section>
  );
}

/** Extract named "## section" blocks from the deterministic context text. */
function pickSections(text: string, names: string[]): string {
  const out: string[] = [];
  const blocks = text.split(/\n(?=## )/);
  for (const block of blocks) {
    const header = /^## (\S+)/.exec(block);
    if (header && names.includes(header[1] ?? "")) out.push(block.trim());
  }
  return out.join("\n\n");
}
