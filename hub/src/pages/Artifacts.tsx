// Artifacts: availability and backup coverage of every source version (A02, CP10.4;
// decision 0018). A digest is not a backup: reference-only bytes (a file PrometheOS only knows
// by its digest) are never shown as backed up, and a reference-only file that was removed shows
// as unavailable. "Check now" records one observation of the origin through the same
// operation as `prometheos source check --save`.
import { useEffect, useState } from "react";
import { describeError, get, post, q, uuid4 } from "../api";
import { OrphansPanel } from "../components/parity";
import { ErrorState, KeyValues, PageHeading, QueryView, formatDate } from "../components/ui";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { CoverageItem, CoverageView, SourceCheckResult } from "../types";

const AVAILABILITY_TEXT: Record<CoverageItem["availability"], string> = {
  retained: "Retained",
  reference_only: "Reference only",
  unavailable: "Unavailable",
};

function CheckButton({ item, onDone }: { item: CoverageItem; onDone: () => void }) {
  const { announce, refresh } = useHub();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SourceCheckResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const done = await post<SourceCheckResult>("/sources/check", {
        ids: [item.version_id],
        save: true,
        request_id: uuid4(),
      });
      setResult(done);
      announce("Check recorded.");
      onDone();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const seen = result?.observations[0];
  return (
    <div className="button-row">
      <button type="button" className="button secondary small" disabled={busy} onClick={() => void run()} data-testid="check-now">
        {busy ? "Checking…" : "Check now and record it"}
      </button>
      {seen ? (
        <span className="muted" data-testid="check-result">
          Origin {seen.origin_reachable ? "reachable" : "not reachable"}
          {seen.origin_matches_version === false ? "; its content differs from the cited snapshot" : ""}.
        </span>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </div>
  );
}

const PAGE = 50;

/** A sentence that three or more cards would repeat is said once above the list, named by
 * what it describes (decision 0078; review F7). */
function sharedReasons(items: CoverageItem[]): Map<string, string> {
  const counts = new Map<string, [string, number]>();
  for (const item of items) {
    for (const [aspect, reason] of [["Availability", item.availability_reason], ["Backup", item.backup.reason]] as const) {
      const [, n] = counts.get(reason) ?? [aspect, 0];
      counts.set(reason, [aspect, n + 1]);
    }
  }
  return new Map([...counts].filter(([, [, n]]) => n >= 3).map(([reason, [aspect]]) => [reason, aspect]));
}

export function ArtifactsPage() {
  // Newest first, 50 at a time; the totals are the project's, computed by the server over every
  // version, so they stay true however many pages are shown (review F7).
  const first = useQuery<CoverageView>(`/artifacts/coverage${q({ limit: PAGE, newest_first: "true" })}`);
  const [more, setMore] = useState<CoverageItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  useEffect(() => {
    // A reload of the first page (a change in the project) restarts the extra pages.
    setMore([]);
    setCursor(first.data?.next_cursor ?? null);
  }, [first.data]);
  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const next = await get<CoverageView>(
        `/artifacts/coverage${q({ limit: PAGE, newest_first: "true", cursor })}`,
      );
      setMore((items) => [...items, ...next.items]);
      setCursor(next.next_cursor);
    } catch (err) {
      setMoreError(err);
    } finally {
      setLoadingMore(false);
    }
  };
  const coverage = first;
  return (
    <>
      <PageHeading
        title="Artifacts"
        lead="Whether each source version's bytes are available, and whether a verified backup holds them."
      />
      <QueryView
        query={coverage}
        loadingLabel="Loading artifacts"
        isEmpty={(d) => d.items.length === 0}
        empty={<>No sources have been imported yet.</>}
      >
        {(view) => {
          const items = [...view.items, ...more];
          const shared = sharedReasons(items);
          return (
          <>
            <p data-testid="coverage-totals" className="muted">
              Whole project, {view.totals.versions} version(s): {view.totals.retained} retained,{" "}
              {view.totals.reference_only} reference only, {view.totals.unavailable} unavailable.{" "}
              {view.totals.backed_up} backed up, {view.totals.not_backed_up} not backed up.{" "}
              {view.verified_backups} verified backup(s) recorded.
            </p>
            {shared.size ? (
              <p className="muted" data-testid="shared-reasons">
                Unless a card says otherwise: {[...shared].map(([reason, aspect]) => `${aspect}: ${reason}`).join(". ")}.
              </p>
            ) : null}
            <ul className="card-list" data-testid="artifacts">
              {items.map((item) => (
                <li
                  key={item.version_id}
                  className="card"
                  data-testid="artifact-card"
                  data-version-id={item.version_id}
                  data-availability={item.availability}
                  data-backup={item.backup.status}
                >
                  <div className="card-head">
                    <p className="card-title">{item.title}</p>
                    <span className={`badge badge-${item.availability}`} data-testid="availability">
                      {AVAILABILITY_TEXT[item.availability]}
                    </span>{" "}
                    <span className={`badge badge-${item.backup.status}`} data-testid="backup-status">
                      {item.backup.covered ? "Backed up" : "Not backed up"}
                    </span>
                  </div>
                  {shared.has(item.availability_reason) ? null : <p>{item.availability_reason}.</p>}
                  {shared.has(item.backup.reason) ? null : <p className="muted">{item.backup.reason}.</p>}
                  {item.origin_changed ? (
                    <p className="inline-warning">
                      The origin file differed from this version when it was last checked; the version
                      keeps citing the snapshot.
                    </p>
                  ) : null}
                  {item.storage === "external" ? <CheckButton item={item} onDone={coverage.reload} /> : null}
                  <details className="details">
                    <summary>Details</summary>
                    <div className="details-body">
                      <KeyValues
                        rows={[
                          ["Version", <code key="v">{item.version_id}</code>],
                          ["Type", item.mime],
                          ["Size", `${item.byte_length} bytes`],
                          ["Storage", item.storage === "external" ? "reference only" : item.storage],
                          [
                            "Held by",
                            item.backup.backups.length
                              ? item.backup.backups
                                  .map((b) => `${b.label} (${b.purpose}, ${formatDate(b.created_at)})`)
                                  .join("; ")
                              : "no verified backup",
                          ],
                        ]}
                      />
                    </div>
                  </details>
                </li>
              ))}
            </ul>
            {cursor ? (
              <div className="button-row">
                <button
                  type="button"
                  className="button secondary"
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                  data-testid="artifacts-more"
                >
                  {loadingMore ? "Loading…" : "Show older versions"}
                </button>
                {moreError ? <ErrorState error={moreError} /> : null}
              </div>
            ) : null}
          </>
          );
        }}
      </QueryView>
      <OrphansPanel />
      <p>
        <Link to="/now">Back to Now</Link>
      </p>
    </>
  );
}
