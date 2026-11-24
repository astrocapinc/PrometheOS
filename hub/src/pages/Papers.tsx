// Papers: the papers in this project with their readiness at a glance, the recurring-check
// inbox, and which claims the record supports. Readiness comes from the pure paper check
// (deterministic findings, never a score); a paper's own page shows the detail.
import { useState } from "react";
import { describeError, post } from "../api";
import {
  ErrorState,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  formatDate,
  humanizeIds,
} from "../components/ui";
import { JobsPanel } from "../components/parity";
import { useKindList } from "../data";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import { stageSummary } from "../stage";
import type {
  ResearchMapView,
  ClaimBody,
  ClaimStatusItem,
  ClaimStatusName,
  ClaimStatusView,
  CorrectionBody,
  Envelope,
  Inbox,
  PaperBody,
  PaperCheck,
  SourceBody,
} from "../types";

export function PapersPage() {
  const papers = useKindList<PaperBody>("paper");
  const claims = useKindList<ClaimBody>("claim");
  const statuses = useQuery<ClaimStatusView>("/knowledge/claim-status");
  const statusOf = new Map((statuses.data?.claims ?? []).map((s) => [s.claim_id, s]));
  const corrections = useKindList<CorrectionBody>("correction");
  const sources = useKindList<SourceBody>("source");
  const title = (sid: string) => sources.data?.items.find((s) => s.id === sid)?.body.title ?? "Source";

  return (
    <>
      <PageHeading
        title="Papers"
        lead="Each paper's readiness, the claims it may use and the evidence behind each."
      />
      <p className="inline-info" data-testid="papers-scope">
        Readiness is a deterministic check of the record (unchecked venue requirements, corrected
        claims still in use, anchors to review, missing files, open reviewer comments). It is not
        a score and never certifies compliance. Nothing here is submitted anywhere.
      </p>
      <section aria-labelledby="papers-title" className="section compact">
        <h2 id="papers-title">Papers in this project</h2>
        <QueryView
          query={papers}
          loadingLabel="Loading papers"
          isEmpty={(d) => d.items.length === 0}
          empty={<>No papers yet. An agent or `prometheos apply` can create one.</>}
        >
          {(data) => (
            <ul className="card-list" data-testid="paper-list">
              {data.items.map((p) => (
                <PaperCard key={p.id} paper={p} />
              ))}
            </ul>
          )}
        </QueryView>
      </section>
      <RecurringChecks />
      <JobsPanel papers={(papers.data?.items ?? []).map((p) => ({ id: p.id, title: p.body.title }))} />
      <section aria-labelledby="map-title" className="section">
        <h2 id="map-title">Claims and their evidence</h2>
        <QueryView
          query={claims}
          loadingLabel="Loading claims"
          isEmpty={(d) => d.items.length === 0}
          empty={<>No claims yet, so there is nothing to write about.</>}
        >
          {(data) => (
            <div className="table-wrap">
              <table className="evidence-table" data-testid="claim-evidence">
                <caption className="visually-hidden">Claims with their state and cited evidence</caption>
                <thead>
                  <tr>
                    <th scope="col">Claim</th>
                    <th scope="col">State</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Use in a paper</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((c) => {
                    const pending = (corrections.data?.items ?? []).some(
                      (x) => x.body.status === "proposed" && x.body.target_refs.some((t) => t.id === c.id),
                    );
                    return (
                      <tr key={c.id}>
                        <th scope="row" className="claim-cell">
                          <Link to={`/knowledge#${c.id}`}>{c.body.statement}</Link>
                        </th>
                        <td>
                          <StateBadge state={c.body.state} />
                        </td>
                        <td>
                          {c.body.evidence.length === 0 ? (
                            <span className="muted">No evidence (gap)</span>
                          ) : (
                            <ul className="plain-list">
                              {c.body.evidence.map((ev, i) => (
                                <li key={i}>
                                  <SourceButton
                                    spec={{
                                      versionId: ev.version_id,
                                      title: title(ev.source_id),
                                      startLine: ev.locator.start_line,
                                      endLine: ev.locator.end_line,
                                      citedBy: c.body.statement,
                                    }}
                                  />
                                </li>
                              ))}
                            </ul>
                          )}
                        </td>
                        <td>
                          {statusOf.has(c.id) ? <ClaimStatusCell item={statusOf.get(c.id)!} /> : usage(c, pending)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryView>
      </section>
    </>
  );
}

const STATUS_TEXT: Record<ClaimStatusName, string> = {
  holds: "Holds",
  corrected: "Holds with a corrected number",
  settling: "Settling: needs a correction or your decision",
  unverified: "Unverified: no check recorded",
  conflicting: "Conflicting values: check before quoting",
  stale: "Stale: do not quote",
  superseded: "Do not use (superseded)",
  withdrawn: "Do not use (withdrawn)",
};

const BASIS_TEXT: Record<string, string> = {
  reviewed: "reviewed by you",
  deterministic: "a deterministic check",
  agent_reported: "your agent's check, not yet reviewed",
};

/** The derived status of a claim (A06.1, CP20.1): what it says, on what basis, and why. It is a
 * view of the record, never support; a stale, superseded or withdrawn claim is never usable. */
export function ClaimStatusCell({
  item,
  reason,
}: {
  item: Pick<ClaimStatusItem, "status" | "basis" | "usable" | "causes">;
  /** A one-line reason worded without file names (the research map's; CP20.2 F9). */
  reason?: string;
}) {
  const found = item.causes.find((c) => c.code !== "supported");
  const cause = found && reason !== undefined ? { ...found, message: reason } : found;
  return (
    <span data-testid="claim-status" data-status={item.status} data-usable={item.usable ? "yes" : "no"}>
      <strong>{STATUS_TEXT[item.status]}</strong>
      {item.basis ? <span className="muted"> ({BASIS_TEXT[item.basis]})</span> : null}
      {cause && item.status !== "holds" ? <span className="muted"> · {cause.message}</span> : null}
    </span>
  );
}

function usage(c: Envelope<ClaimBody>, pending: boolean): string {
  if (c.body.state === "superseded" || c.body.state === "withdrawn") return "Do not use (historical)";
  if (pending) return "Hold: a correction is proposed";
  if (c.body.evidence.length === 0) return "Needs evidence";
  if (c.body.state === "contested") return "Contested: discuss limits";
  return "Usable within its stated scope";
}

function PaperCard({ paper }: { paper: Envelope<PaperBody> }) {
  const check = useQuery<PaperCheck>(`/papers/${encodeURIComponent(paper.id)}/check`);
  const map = useQuery<ResearchMapView>(
    `/knowledge/research-map?paper_id=${encodeURIComponent(paper.id)}`,
  );
  const deliverable = map.data?.deliverables[0];
  const total = check.data ? check.data.findings.length : null;
  return (
    <li className="card" data-testid="paper-card">
      <div className="card-head">
        <Link to={`/papers/${paper.id}`} className="card-title">
          {paper.body.title}
        </Link>
        <StateBadge state={paper.body.status} prefix="Status" />
      </div>
      <p className="card-meta">
        {paper.body.deadline ? <>Deadline {formatDate(paper.body.deadline)} · </> : null}
        {paper.body.venue_source_refs.length === 0 ? "Venue: unknown" : "Venue guidance recorded"}
        {" · "}
        {check.error ? (
          <span>readiness unavailable</span>
        ) : total === null ? (
          <span>checking…</span>
        ) : total === 0 ? (
          <span data-testid="paper-findings">no open readiness findings</span>
        ) : (
          <span data-testid="paper-findings">
            {total} readiness finding{total === 1 ? "" : "s"} to look at
          </span>
        )}
      </p>
      {deliverable ? (
        <p className="card-meta" data-testid="paper-stage">
          Stage: {stageSummary(deliverable)}
        </p>
      ) : null}
    </li>
  );
}

const CATEGORY_LABELS: Record<string, string> = {
  finding: "Change found",
  digest: "Project digest",
  slot_unknown: "Outcome unknown — decide",
  slot_failed: "Check failed",
};

function RecurringChecks() {
  const inbox = useQuery<Inbox>("/inbox");
  const { announce, refresh } = useHub();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [ran, setRan] = useState<string | null>(null);

  const runNow = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await post<{ ran: { state: string }[] }>("/jobs/tick", {});
      const text =
        result.ran.length === 0
          ? "No recurring check was due."
          : `${result.ran.length} due check(s) ran.`;
      setRan(text);
      announce(text);
      await refresh();
      inbox.reload();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async (id: number) => {
    try {
      await post(`/inbox/${id}/dismiss`, {});
      inbox.reload();
      announce("Dismissed.");
    } catch (err) {
      setError(err);
    }
  };

  return (
    <section aria-labelledby="checks-title" className="section compact">
      <h2 id="checks-title">Recurring checks</h2>
      <p className="muted">
        Paper readiness, source checks and project digests run only while `prometheos jobs tick`
        (or this hub) runs; unchanged results stay quiet.
      </p>
      <button
        type="button"
        className="button secondary small"
        onClick={() => void runNow()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy ? "Running…" : "Run due checks now"}
      </button>
      {ran ? (
        <p className="info" data-testid="tick-result">
          {ran}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      <QueryView
        query={inbox}
        loadingLabel="Loading inbox"
        isEmpty={(d) => d.items.length === 0}
        empty={<>Nothing new from recurring checks.</>}
      >
        {(data) => (
          <ul className="card-list" data-testid="inbox">
            {data.items.map((item) => (
              <li key={item.id} className="card" data-testid="inbox-item">
                <div className="card-head">
                  <span className="card-title">{humanizeIds(item.title)}</span>
                  <span className="badge badge-category">
                    {CATEGORY_LABELS[item.category] ?? item.category}
                  </span>
                </div>
                <p className="card-meta">For the slot due {formatDate(item.due_utc)}</p>
                <ul className="plain-list">
                  {item.summary.split("\n").map((line, i) => (
                    <li key={i}>{humanizeIds(line)}</li>
                  ))}
                </ul>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => void dismiss(item.id)}
                  aria-label={`Dismiss: ${humanizeIds(item.title)}`}
                >
                  Dismiss
                </button>
              </li>
            ))}
          </ul>
        )}
      </QueryView>
    </section>
  );
}
