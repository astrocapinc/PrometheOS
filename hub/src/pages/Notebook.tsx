// Notebook: readable dated entries with source indicators and correction badges.
// Exact sources open in the context panel; IDs and revisions sit behind Details.
import { useMemo, useState } from "react";
import { get } from "../api";
import {
  Details,
  ErrorState,
  KeyValues,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  formatDate,
  formatDay,
  stateLabel,
} from "../components/ui";
import { NotebookExport } from "../components/parity";
import { useKindList } from "../data";
import { useQuery } from "../hub";
import { Link, useHashTarget } from "../router";
import type { EntryBody, MeetingBody, Notebook, NotebookEntry } from "../types";

const CATEGORY_LABELS: Record<string, string> = {
  note: "Note",
  finding: "Finding",
  experiment: "Experiment",
  decision: "Decision",
  question: "Question",
};

const ORIGIN_LABELS: Record<string, string> = {
  typed: "Typed by you",
  imported: "Imported",
  agent: "Written by an agent",
  generated: "Generated wording",
};

export function NotebookPage() {
  const notebook = useQuery<Notebook>("/notebook");
  const envelopes = useKindList<EntryBody>("entry");
  const recordedAt = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of envelopes.data?.items ?? []) map.set(e.id, e.occurred_at ?? e.recorded_at);
    return map;
  }, [envelopes.data]);
  useHashTarget(notebook.data !== undefined && envelopes.data !== undefined);

  const days = useMemo(() => {
    const entries = [...(notebook.data?.entries ?? [])].sort((a, b) => b.recorded_seq - a.recorded_seq);
    const groups: { day: string; entries: NotebookEntry[] }[] = [];
    for (const entry of entries) {
      const when = entry.occurred_at ?? recordedAt.get(entry.id) ?? null;
      const day = formatDay(when);
      const last = groups[groups.length - 1];
      if (last && last.day === day) last.entries.push(entry);
      else groups.push({ day, entries: [entry] });
    }
    return groups;
  }, [notebook.data, recordedAt]);

  return (
    <>
      <PageHeading title="Notebook" lead="Dated entries, with the sources each one cites." />
      <MeetingsList />
      <NotebookExport />
      <section aria-labelledby="entries-title" className="section">
        <h2 id="entries-title">Entries</h2>
        <QueryView
          query={notebook}
          loadingLabel="Loading notebook"
          isEmpty={(d) => d.entries.length === 0}
          empty={
            <>
              The notebook is empty. <Link to="/capture">Capture a note</Link> to start it.
            </>
          }
        >
          {() =>
            days.map((group) => (
              <section key={group.day} className="day" aria-label={group.day}>
                <h3 className="day-heading">{group.day}</h3>
                <ol className="card-list">
                  {group.entries.map((entry) => (
                    <EntryCard key={entry.id} entry={entry} when={recordedAt.get(entry.id)} />
                  ))}
                </ol>
              </section>
            ))
          }
        </QueryView>
      </section>
    </>
  );
}

function MeetingsList() {
  const meetings = useKindList<MeetingBody>("meeting");
  return (
    <section aria-labelledby="meetings-title" className="section compact">
      <h2 id="meetings-title">Meeting notes</h2>
      <QueryView
        query={meetings}
        loadingLabel="Loading meetings"
        isEmpty={(d) => d.items.length === 0}
        empty={
          <>
            No meetings yet. <Link to="/capture?type=meeting">Capture meeting notes</Link>.
          </>
        }
      >
        {(data) => (
          <ul className="plain-list">
            {[...data.items]
              .sort((a, b) => ((a.body.held_at ?? a.recorded_at) < (b.body.held_at ?? b.recorded_at) ? 1 : -1))
              .map((m) => (
                <li key={m.id} className="row">
                  <Link to={`/meetings/${m.id}`}>{m.body.title}</Link>{" "}
                  <span className="muted">{formatDate(m.body.held_at ?? m.recorded_at)}</span>{" "}
                  <StateBadge state={m.body.processing} />
                </li>
              ))}
          </ul>
        )}
      </QueryView>
    </section>
  );
}

function EntryCard({ entry, when }: { entry: NotebookEntry; when?: string }) {
  const corrected = entry.correction_notice != null;
  const needsReview = entry.needs_review != null;
  return (
    <li className="card entry-card" id={entry.id} data-testid="entry-card">
      <div className="card-head">
        <h4 className="card-title">{entry.title}</h4>
        <span className="badge badge-category">{CATEGORY_LABELS[entry.category] ?? entry.category}</span>
      </div>
      <p className="card-meta">
        {formatDate(when)} · {ORIGIN_LABELS[entry.origin] ?? entry.origin} · {stateLabel(entry.review)}
        {corrected ? (
          <>
            {" "}
            · <span className="badge badge-correction" data-testid="correction-badge">Corrected</span>
          </>
        ) : null}
        {needsReview ? (
          <>
            {" "}
            · <span className="badge badge-review">Needs review</span>
          </>
        ) : null}
      </p>
      <p className="entry-text">{entry.text}</p>
      {corrected ? <p className="inline-warning">{noticeText(entry.correction_notice)}</p> : null}
      {needsReview ? <p className="inline-warning">{noticeText(entry.needs_review)}</p> : null}
      <div className="sources">
        <span className="sources-label">Sources:</span>{" "}
        {entry.sources.length === 0 ? (
          <span className="muted" data-testid="no-source">
            no source cited
          </span>
        ) : (
          <ul className="inline-list">
            {entry.sources.map((s, i) => (
              <li key={i}>
                {s.availability === "retained" ? (
                  <SourceButton
                    spec={{
                      versionId: s.version_id,
                      title: s.title,
                      startLine: s.locator.start_line,
                      endLine: s.locator.end_line,
                      locatorLabel: s.locator_label ?? undefined,
                      citedBy: entry.title,
                    }}
                  />
                ) : (
                  <SourceButton
                    spec={{
                      versionId: s.version_id,
                      title: s.title,
                      startLine: s.locator.start_line,
                      endLine: s.locator.end_line,
                      locatorLabel: s.locator_label ?? undefined,
                      citedBy: entry.title,
                    }}
                  >
                    <span aria-hidden="true">¶ </span>
                    {s.title} ({stateLabel(s.availability)})
                  </SourceButton>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <Details>
        <KeyValues
          rows={[
            ["Entry", <code key="e">{entry.id}</code>],
            ["Revision", String(entry.rev)],
            ["Recorded at project sequence", String(entry.recorded_seq)],
          ]}
        />
        <History id={entry.id} />
      </Details>
    </li>
  );
}

function noticeText(notice: unknown): string {
  if (typeof notice === "string") return notice;
  if (notice && typeof notice === "object") {
    const n = notice as Record<string, unknown>;
    const text = n.text ?? n.message ?? n.reason ?? n.summary;
    if (typeof text === "string") return text;
  }
  return "A correction affects this entry.";
}

interface HistoryReply {
  revisions: { rev: number; operation: string; recorded_at: string; commit_seq: number }[];
}

function History({ id }: { id: string }) {
  const [state, setState] = useState<{ data?: HistoryReply; error?: unknown; loading: boolean }>({
    loading: false,
  });
  const load = async () => {
    setState({ loading: true });
    try {
      setState({ data: await get<HistoryReply>(`/records/${encodeURIComponent(id)}/history`), loading: false });
    } catch (error) {
      setState({ error, loading: false });
    }
  };
  if (state.error) return <ErrorState error={state.error} onRetry={() => void load()} />;
  if (!state.data) {
    return (
      <button type="button" className="link-button" onClick={() => void load()} disabled={state.loading}>
        {state.loading ? "Loading revisions…" : "Show revision history"}
      </button>
    );
  }
  return (
    <ol className="history">
      {state.data.revisions.map((r) => (
        <li key={r.rev}>
          Revision {r.rev}: {r.operation.replace(/_/g, " ")} · {formatDate(r.recorded_at)} (sequence{" "}
          {r.commit_seq})
        </li>
      ))}
    </ol>
  );
}
