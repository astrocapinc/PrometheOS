// Now: current objective with the deliverables and their derived stages (A06.1, CP20.2), changes
// since the last visit, next adopted actions (with why and evidence) and the one prominent
// Capture action.
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { describeError, post, uuid4 } from "../api";
import {
  Details,
  Empty,
  ErrorState,
  KeyValues,
  Loading,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  humanizeIds,
  kindLabel,
} from "../components/ui";
import { readLastSeen, recordPath, recordTitle, useKindList, useRecords, writeLastSeen } from "../data";
import { Icon } from "../components/icons";
import { useHub, useQuery, useRunWatch, type QueryState } from "../hub";
import { ownerZone, zonedText } from "../deadline";
import { Link } from "../router";
import { splitChanged } from "../fold";
import { DecisionCards, PlannedChecks } from "../components/Checks";
import { DeliverableCard, MapOverview } from "../components/ResearchMap";
import { EventInbox } from "./Inbox";
import { Deadline, Labels } from "./Work";
import type {
  ResultDraft,
  ActionBody,
  BriefGroup,
  BriefItem,
  CapabilitiesView,
  Changes,
  CorrectionBody,
  Envelope,
  ItemBody,
  NextSteps,
  NowItem,
  ExecutionList,
  NowSection,
  NowView,
  ResearchMapView,
  ResolveResult,
  SourceBody,
} from "../types";

const ADOPTED = new Set(["ready", "in_progress", "blocked"]);
const MEANINGFUL = new Set(["entry", "claim", "correction", "item", "action", "meeting", "source"]);

export function NowPage() {
  const { project } = useHub();
  // One /now read feeds the glance strip and "What needs attention". Approval requests live
  // outside the project sequence, so it polls; it pauses while the tab is hidden (review F10).
  const now = useQuery<NowView>("/now");
  const reload = now.reload;
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, NOW_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  // A run that starts, progresses or ends shows here without waiting for the poll (0076).
  useRunWatch(reload, 3000);
  return (
    <>
      <PageHeading title="Now" />
      <section className="now-hero" aria-label="Current objective">
        <div className="now-objective">
          <span className="lead-label">Current objective</span>
          {project ? (
            <span className="objective" data-testid="objective">
              {project.settings.current_objective}
            </span>
          ) : (
            <span className="objective muted">Loading the project…</span>
          )}
        </div>
        <div className="capture-cta">
          <Link to="/capture" className="button primary large" data-testid="capture-cta">
            <Icon name="plus" />
            Capture a note or meeting
          </Link>
          <p className="muted">Typed notes and meeting notes go straight into the project record.</p>
        </div>
      </section>
      <Brief view={now.data} />
      <Deliverables now={now.data} />
      <div className="now-grid now-decide">
        <div className="now-col">
          <DecisionCards onChanged={reload} />
        </div>
        <div className="now-col">
          <PlannedChecks />
        </div>
      </div>
      <Glance view={now.data} />
      <div className="now-grid">
        <div className="now-col">
          <Continuity now={now} />
          <EventInbox />
        </div>
        <div className="now-col">
          <NextActions />
          <AwaitingDecision />
          <SinceLastVisit />
        </div>
      </div>
    </>
  );
}

/** The project landing (A06.1, CP20.2): each deliverable with its derived stage and what moves
 * it forward, then the rest of the research map (CP20.4): the objective's own questions, the
 * questions placed nowhere yet and the tracks. The decision cards and planned checks follow this
 * section (CP20.3, decision 0084); the count here links to the approvals. */
function Deliverables({ now }: { now: NowView | undefined }) {
  const map = useQuery<ResearchMapView>("/knowledge/research-map");
  const reload = map.reload;
  const seq = now?.project_seq;
  useEffect(() => {
    if (seq !== undefined) reload();
  }, [seq, reload]);
  const waiting = now?.approvals.count ?? 0;
  return (
    <section className="section deliverables" aria-labelledby="deliverables-title">
      <h2 id="deliverables-title">Deliverables</h2>
      {waiting > 0 ? (
        <p data-testid="decisions-waiting">
          <a href="#now-approvals">
            {waiting} decision{waiting === 1 ? "" : "s"} waiting for you
          </a>
        </p>
      ) : null}
      <QueryView query={map} loadingLabel="Loading deliverables">
        {(data) => {
          // A withdrawn paper is no longer a deliverable on the landing (review cp20-2-a F11).
          const open = data.deliverables.filter((d) => d.paper_status !== "withdrawn");
          return (
            <>
              {open.length === 0 ? (
                <Empty>No papers recorded yet.</Empty>
              ) : (
                <ul className="deliverable-list">
                  {open.map((d) => (
                    <DeliverableCard key={d.paper_id} deliverable={d} />
                  ))}
                </ul>
              )}
              <MapOverview map={data} onChanged={reload} />
            </>
          );
        }}
      </QueryView>
    </section>
  );
}

const BRIEF_VERB: Record<BriefGroup, string> = {
  decide: "Decide",
  due: "Due",
  evidence: "Claim",
  interpret: "Read",
};
const BRIEF_EMPTY: Record<BriefGroup, string> = {
  decide: "decisions",
  due: "due work",
  evidence: "claims to fix",
  interpret: "results to read",
};

function briefLink(item: BriefItem): string | null {
  if (item.kind === "execution" || item.kind === "version") return "#now-awaiting";
  if (item.kind === "approval_request") return `/approvals/${item.id}`;
  return recordPath({ id: item.id, kind: item.kind, body: {} });
}

/** The brief (decision 0109): what needs you now, at most five lines in a fixed order with the
 * reason for each; the groups with nothing in them share one short line instead of boxes. */
function Brief({ view }: { view: NowView | undefined }) {
  const { project } = useHub();
  const zone = ownerZone(project);
  const brief = view?.brief;
  if (!brief) return null;
  const groups = Object.keys(BRIEF_VERB) as BriefGroup[];
  const empty = groups.filter((g) => brief.counts[g] === 0).map((g) => BRIEF_EMPTY[g]);
  return (
    <section className="section brief" aria-labelledby="brief-title" data-testid="now-brief">
      <h2 id="brief-title">
        What needs you now
        {brief.total ? <span className="muted brief-total"> · {brief.total}</span> : null}
      </h2>
      {brief.items.length ? (
        <ol className="brief-list">
          {brief.items.map((item) => {
            const to = briefLink(item);
            return (
              <li key={`${item.group}:${item.id}`} className={`brief-item brief-${item.group}`} data-testid="brief-item">
                <span className="brief-verb">{BRIEF_VERB[item.group]}</span>
                <span className="brief-text">
                  {to ? (
                    to.startsWith("#") ? <a href={to}>{item.title}</a> : <Link to={to}>{item.title}</Link>
                  ) : (
                    item.title
                  )}
                  <span className="brief-why muted"> {zonedText(humanizeIds(item.why), zone)}</span>
                </span>
              </li>
            );
          })}
        </ol>
      ) : (
        <p data-testid="brief-clear">Nothing needs you right now.</p>
      )}
      {brief.total > brief.items.length ? (
        <p className="muted small-print">
          {brief.total - brief.items.length} more: see the lists below and the map.
        </p>
      ) : null}
      {brief.items.length && empty.length ? (
        <p className="muted small-print" data-testid="brief-nothing">
          Nothing yet: {empty.join(", ")}.
        </p>
      ) : null}
      <p className="muted small-print">Order: {brief.rule}. Computed from the record, not by a model.</p>
    </section>
  );
}

/** At a glance: the counts behind "What needs attention", each linking to its list. Tiles with
 * nothing in them are not drawn; when all are empty, one line says so (decision 0109). */
function Glance({ view }: { view: NowView | undefined }) {
  const tiles: [string, string, number | undefined][] = [
    ["now-approvals", "Waiting for your approval", view?.approvals.count],
    ["now-awaiting", "Results to interpret", view?.awaiting_interpretation.count],
    ["now-running", "Running", view?.running.count],
    ["now-due", "Due soon", view?.due.count],
    ["now-changed", "Need review after a change", view ? splitChanged(view.changed).flagged.count : undefined],
    ["now-checked-by-agent", "Checked by your agent", view ? splitChanged(view.changed).checkedTotal : undefined],
  ];
  const shown = view ? tiles.filter(([, , count]) => count) : tiles;
  if (view && shown.length === 0) {
    return (
      <p className="muted glance-clear" data-testid="now-glance">
        Nothing waiting for approval, nothing to interpret, running, due or flagged after a change.
      </p>
    );
  }
  return (
    <ul className="glance" aria-label="At a glance" data-testid="now-glance">
      {shown.map(([target, label, count]) => (
        <li key={target} className={count ? "glance-tile active" : "glance-tile"}>
          <a href={`#${target}`}>
            {count === undefined ? (
              <span className="glance-count">
                <span aria-hidden="true">–</span>
                <span className="visually-hidden">Not loaded</span>
              </span>
            ) : (
              <span className="glance-count">{count}</span>
            )}
            <span className="glance-label">{label}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function useSourceTitles() {
  const sources = useKindList<SourceBody>("source");
  return useMemo(() => {
    const map = new Map<string, string>();
    for (const s of sources.data?.items ?? []) map.set(s.id, s.body.title);
    return map;
  }, [sources.data]);
}

function NextActions() {
  const { project } = useHub();
  const zone = ownerZone(project);
  const actions = useKindList<ActionBody>("action");
  const next = useQuery<NextSteps>("/next");
  const items = useKindList<ItemBody>("item");
  const sourceTitles = useSourceTitles();
  const whyById = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of next.data?.candidates ?? []) {
      if (c.adopted_action_id) map.set(c.adopted_action_id, c.why_now);
    }
    return map;
  }, [next.data]);
  const itemsById = useMemo(() => {
    const map = new Map<string, Envelope<ItemBody>>();
    for (const i of items.data?.items ?? []) map.set(i.id, i);
    return map;
  }, [items.data]);

  return (
    <section aria-labelledby="next-actions" className="section">
      <h2 id="next-actions">Next adopted actions</h2>
      <QueryView
        query={actions}
        loadingLabel="Loading actions"
        isEmpty={(d) => d.items.filter((a) => ADOPTED.has(a.body.status)).length === 0}
        empty={
          <>
            No adopted actions yet. Adopt a meeting commitment to make it the next step here.{" "}
            <Link to="/capture" className="empty-action">
              Capture meeting notes
            </Link>
          </>
        }
      >
        {(data) => (
          <ol className="card-list" data-testid="next-actions">
            {data.items
              .filter((a) => ADOPTED.has(a.body.status))
              .sort((a, b) => (a.recorded_at < b.recorded_at ? 1 : -1))
              .map((action) => {
                const item = action.body.commitment_item_id
                  ? itemsById.get(action.body.commitment_item_id)
                  : undefined;
                return (
                  <li key={action.id} className="card" data-testid="action-card">
                    <div className="card-head">
                      <h3 className="card-title">
                        <Link to={`/actions/${action.id}`}>{action.body.title}</Link>
                      </h3>
                      <StateBadge state={action.body.status} prefix="Action state" />
                    </div>
                    <p className="card-meta">
                      {action.body.owner_ref ? "Owner recorded" : "No owner recorded"} ·{" "}
                      {action.body.due_at ? (
                        <Deadline due={action.body.due_at} zone={zone} status={action.body.status} />
                      ) : (
                        "No agreed date"
                      )}
                      {action.body.blocker ? <> · Blocked: {action.body.blocker}</> : null}
                    </p>
                    <Labels labels={action.body.labels} />
                    <details className="details why">
                      <summary>Why this action and its evidence</summary>
                      <div className="details-body">
                        <p>
                          <strong>Reason:</strong> {action.body.reason}
                        </p>
                        {whyById.get(action.id) ? (
                          <p>
                            <strong>Why now:</strong> {humanizeIds(whyById.get(action.id) ?? "")}
                          </p>
                        ) : null}
                        {item ? (
                          <div className="evidence-block">
                            <p>
                              <strong>From meeting commitment:</strong> “{item.body.text}”
                            </p>
                            <ul className="source-list">
                              {item.body.evidence.map((ev, i) => (
                                <li key={i}>
                                  <SourceButton
                                    spec={{
                                      versionId: ev.version_id,
                                      title: sourceTitles.get(ev.source_id) ?? "Meeting notes",
                                      startLine: ev.locator.start_line,
                                      endLine: ev.locator.end_line,
                                      startMs: ev.locator.start_ms as number | undefined,
                                      endMs: ev.locator.end_ms as number | undefined,
                                      citedBy: item.body.text,
                                    }}
                                  />
                                </li>
                              ))}
                            </ul>
                            {item.body.meeting_id ? (
                              <p>
                                <Link to={`/meetings/${item.body.meeting_id}`}>Open the meeting</Link>
                              </p>
                            ) : null}
                          </div>
                        ) : (
                          <p className="muted">No meeting commitment is linked to this action.</p>
                        )}
                      </div>
                    </details>
                  </li>
                );
              })}
          </ol>
        )}
      </QueryView>
    </section>
  );
}

function AwaitingDecision() {
  const next = useQuery<NextSteps>("/next");
  const items = useKindList<ItemBody>("item");
  const corrections = useKindList<CorrectionBody>("correction");
  const meetingOf = useMemo(() => {
    const map = new Map<string, string | null>();
    for (const i of items.data?.items ?? []) map.set(i.id, i.body.meeting_id);
    return map;
  }, [items.data]);

  const proposed = (corrections.data?.items ?? []).filter((c) => c.body.status === "proposed");
  const candidates = (next.data?.candidates ?? []).filter((c) => !c.adopted_action_id);
  const loading = next.data === undefined && next.loading;

  return (
    <section aria-labelledby="awaiting" className="section">
      <h2 id="awaiting">Waiting for your decision</h2>
      {loading ? <Loading label="Loading suggestions" /> : null}
      {next.error && next.data === undefined ? (
        <ErrorState error={next.error} onRetry={next.reload} />
      ) : null}
      {!loading && candidates.length === 0 && proposed.length === 0 && !next.error ? (
        <Empty>Nothing is waiting for a decision.</Empty>
      ) : null}
      <ul className="plain-list" data-testid="awaiting">
        {proposed.map((c) => (
          <li key={c.id} className="row">
            <StateBadge state="proposed" prefix="Correction" />{" "}
            <span>Proposed correction: {c.body.reason}</span>{" "}
            <Link to={`/corrections/${c.id}`}>Review correction</Link>
          </li>
        ))}
        {candidates.map((c) => {
          const ref = c.source_refs[0];
          const meeting = ref ? meetingOf.get(ref) : null;
          const paper = c.source_refs.find((r) => r.startsWith("paper_"));
          return (
            <li key={c.id} className="row">
              <span>{humanizeIds(c.why_now)}</span>{" "}
              {meeting ? <Link to={`/meetings/${meeting}`}>Review in meeting</Link> : null}
              {paper && !meeting ? <Link to={`/papers/${paper}`}>Open the paper</Link> : null}
              {c.why_now !== humanizeIds(c.why_now) ? (
                <Details>
                  <p>{c.why_now}</p>
                </Details>
              ) : null}
            </li>
          );
        })}
      </ul>
      {next.data ? (
        <p className="muted small-print">
          Suggestions are {next.data.generation}: computed from the record, not by a model.
        </p>
      ) : null}
    </section>
  );
}

function SinceLastVisit() {
  const { project, knownSeq } = useHub();
  const projectId = project?.project_id ?? "";
  const [lastSeen, setLastSeen] = useState<number | null>(() =>
    projectId ? readLastSeen(projectId) : null,
  );
  const effectiveLastSeen = lastSeen ?? (projectId ? readLastSeen(projectId) : null);
  const after = effectiveLastSeen ?? 0;
  const changes = useQuery<Changes>(projectId ? `/changes?after_seq=${after}&wait=0` : null);

  const latestFirst = useMemo(() => {
    const byId = new Map<string, { id: string; kind: string; seq: number; rev: number }>();
    for (const c of changes.data?.changed ?? []) {
      if (!MEANINGFUL.has(c.kind)) continue;
      const prior = byId.get(c.id);
      if (!prior || prior.seq < c.seq) byId.set(c.id, c);
    }
    return [...byId.values()].sort((a, b) => b.seq - a.seq).slice(0, 8);
  }, [changes.data]);
  const records = useRecords(latestFirst.map((c) => c.id));

  const markSeen = () => {
    const seq = changes.data?.latest_seq ?? knownSeq;
    writeLastSeen(projectId, seq);
    setLastSeen(seq);
  };

  return (
    <section aria-labelledby="since" className="section">
      <h2 id="since">
        {effectiveLastSeen === null ? "Recent changes" : "Changes since your last visit"}
      </h2>
      <QueryView
        query={changes}
        loadingLabel="Loading changes"
        isEmpty={() => latestFirst.length === 0}
        empty={<>No changes since you last marked this project as seen.</>}
      >
        {() => (
          <>
            <ol className="change-list" data-testid="changes">
              {latestFirst.map((c, index) => {
                const record = records.records.get(c.id);
                const title = record ? recordTitle(record) : records.loading ? "Loading…" : "Removed record";
                const to = record ? recordPath(record) : null;
                return (
                  <li key={c.id} className={index === 0 ? "change latest" : "change"}>
                    {index === 0 ? <span className="latest-label">Latest: </span> : null}
                    <span className="change-kind">
                      {kindLabel(c.kind)}
                      {c.rev > 1 ? " updated" : " added"}
                    </span>{" "}
                    {to ? <Link to={to}>{title}</Link> : <span>{title}</span>}
                    {record && "status" in record.body ? (
                      <>
                        {" "}
                        <StateBadge state={String((record.body as { status: string }).status)} />
                      </>
                    ) : null}
                    {record && record.kind === "claim" ? (
                      <>
                        {" "}
                        <StateBadge state={String((record.body as { state: string }).state)} />
                      </>
                    ) : null}
                  </li>
                );
              })}
            </ol>
            <div className="button-row">
              <button type="button" className="button secondary" onClick={markSeen}>
                Mark all as seen
              </button>
            </div>
            <Details>
              <KeyValues
                rows={[
                  ["Since project sequence", String(after)],
                  ["Latest project sequence", String(changes.data?.latest_seq ?? knownSeq)],
                ]}
              />
            </Details>
          </>
        )}
      </QueryView>
    </section>
  );
}

// ----- A02, CP10.4: what needs attention, each item with its reason (decision 0018) -----------

const NOW_POLL_MS = 15000;

function NowGroup({
  id,
  title,
  section,
  empty,
  children,
}: {
  id: string;
  title: string;
  section: NowSection;
  empty: string;
  children?: (item: NowItem) => ReactNode;
}) {
  const { project } = useHub();
  const zone = ownerZone(project);
  return (
    <div className={section.count ? "now-group" : "now-group is-empty"} data-testid={id} id={id}>
      <h3 className="subhead">
        {title} <span className="muted">({section.count})</span>
      </h3>
      {section.items.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <ul className="plain-list">
          {section.items.map((item) => {
            const to = recordPath({ id: item.id, kind: item.kind, body: {} });
            return (
              <li
                key={item.id}
                className="row now-item"
                data-testid="now-item"
                data-kind={item.kind}
                data-id={item.id}
              >
                <span className="badge badge-kind">{kindLabel(item.kind)}</span>{" "}
                {to ? <Link to={to}>{item.title}</Link> : <span>{item.title}</span>}
                {item.state ? (
                  <>
                    {" "}
                    <StateBadge state={item.state} prefix="State" />
                  </>
                ) : null}
                <p className="reason" data-testid="now-reason">
                  <strong>Why:</strong> {zonedText(humanizeIds(item.reason), zone)}
                </p>
                {item.due_at ? (
                  <p className="muted" data-testid="now-deadline">
                    <Deadline due={item.due_at} zone={zone} />
                  </p>
                ) : null}
                {children ? children(item) : null}
              </li>
            );
          })}
        </ul>
      )}
      {section.truncated ? (
        <p className="muted">Showing the first {section.items.length} of {section.count}.</p>
      ) : null}
    </div>
  );
}

function signed(value: string): string {
  return value.startsWith("-") || /^0(\.0*)?$/.test(value) ? value : `+${value}`;
}

// Decision 0110 (L2): what a rerun's output says about each bound quantity. PrometheOS reads the
// number from the same labelled line (or from the line the layer proposed, when two models agree);
// it never sets a tolerance. Recording the check stays yours (or your agent's).
function ResultDrafts({ drafts }: { drafts?: ResultDraft[] }) {
  if (!drafts?.length) return null;
  return (
    <ul className="plain-list result-drafts" data-testid="result-drafts">
      {drafts.map((d) => {
        const unit = d.unit ? ` ${d.unit}` : "";
        return (
          <li key={`${d.claim_id}:${d.quantity_index}`} data-testid="result-draft" data-status={d.status}>
            <p className="lead-label">
              Draft reading: {d.name ?? "a bound quantity"} of <q>{d.claim_line}</q>
            </p>
            {d.measured ? (
              <p data-testid="draft-numbers">
                Expected <strong>{d.expected.decimal + unit}</strong>, measured{" "}
                <strong>{d.measured.decimal + unit}</strong> (line {d.measured.line} of the new version):{" "}
                {signed(d.difference ?? "0") + unit}
                {d.relative_difference_percent !== null ? ` (${signed(d.relative_difference_percent)} %)` : ""}
              </p>
            ) : (
              <p className="muted" data-testid="draft-numbers">
                {d.summary}
              </p>
            )}
            <p className="muted small-print" data-testid="draft-source">
              {d.found_by === "layer"
                ? "Line proposed by the intelligence layer (two models agreed; a hint); the number is read by PrometheOS. "
                : d.found_by === "label"
                  ? "Read by PrometheOS from the line with the same label. "
                  : d.layer && !d.layer.agree
                    ? "The models disagree on which line it is. "
                    : ""}
              No tolerance is set: choose one when you record the check.
            </p>
          </li>
        );
      })}
    </ul>
  );
}

function ResolveForm({ item, onDone }: { item: NowItem; onDone: () => void }) {
  const { announce, refresh } = useHub();
  const [text, setText] = useState("");
  const drafted = (item.drafts ?? []).filter((d) => d.measured).map((d) => d.summary);
  const [busy, setBusy] = useState<"interpreted" | "dismissed" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const submit = async (resolution: "interpreted" | "dismissed", event?: FormEvent) => {
    event?.preventDefault();
    if (!text.trim()) {
      setError(new Error("Write the interpretation, or the reason you are dismissing it."));
      return;
    }
    setBusy(resolution);
    setError(null);
    try {
      await post<ResolveResult>(`/awaiting-interpretation/${item.id}/resolve`, {
        resolution,
        text: text.trim(),
        request_id: uuid4(),
      });
      announce(resolution === "dismissed" ? "Dismissed, with your reason recorded." : "Interpretation recorded.");
      setText("");
      onDone();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };
  return (
    <details className="details resolve" data-testid="resolve-details">
      <summary>Record an interpretation or dismiss with a reason</summary>
      <form className="details-body" onSubmit={(e) => void submit("interpreted", e)}>
        <div className="field">
          <label htmlFor={`resolve-${item.id}`}>Interpretation, or the reason to dismiss</label>
          <textarea
            id={`resolve-${item.id}`}
            data-testid="resolve-text"
            rows={3}
            maxLength={65536}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        {drafted.length && !text ? (
          <button
            type="button"
            className="button secondary small"
            data-testid="resolve-from-draft"
            onClick={() => setText(drafted.join(" ") + " Tolerance: ")}
          >
            Start from the draft reading
          </button>
        ) : null}
        <p className="muted small-print">
          Either one is saved as a reviewed notebook entry that cites this result; nothing leaves
          this list silently.
        </p>
        {error ? <ErrorState error={error} /> : null}
        <div className="button-row">
          <button type="submit" className="button primary" disabled={busy !== null} data-testid="resolve-interpreted">
            {busy === "interpreted" ? "Saving…" : "Record interpretation"}
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={busy !== null}
            data-testid="resolve-dismissed"
            onClick={() => void submit("dismissed")}
          >
            {busy === "dismissed" ? "Saving…" : "Dismiss with this reason"}
          </button>
        </div>
      </form>
    </details>
  );
}

function Continuity({ now }: { now: QueryState<NowView> }) {
  const capabilities = useQuery<CapabilitiesView>("/capabilities");
  const reload = now.reload;
  const blockers = (capabilities.data?.features ?? []).filter((f) => !f.available && f.blocker);
  return (
    <section aria-labelledby="attention" className="section" data-testid="now-continuity">
      <h2 id="attention">What needs attention</h2>
      <QueryView query={now} loadingLabel="Loading what needs attention">
        {(view) => {
          // Decision 0109: empty groups share one line instead of a box each.
          const flagged = splitChanged(view.changed).flagged;
          const quiet = [
            [view.approvals.count, "approvals"],
            [view.awaiting_interpretation.count, "results to interpret"],
            [view.running.count, "running"],
            [view.due.count, "due"],
            [flagged.count, "flagged after a change"],
          ]
            .filter(([count]) => !count)
            .map(([, name]) => name);
          return (
          <>
            {quiet.length ? (
              <p className="muted" data-testid="now-quiet">
                Nothing yet: {quiet.join(", ")}.
              </p>
            ) : null}
            {view.approvals.count ? (
            <NowGroup
              id="now-approvals"
              title="Waiting for your approval"
              section={view.approvals}
              empty="No approval requests are waiting."
            >
              {(item) => (
                <p>
                  <Link to={item.owner_route?.hub ?? "/approvals"}>Review the exact request</Link>
                </p>
              )}
            </NowGroup>
            ) : null}
            {view.awaiting_interpretation.count ? (
            <NowGroup
              id="now-awaiting"
              title="Results awaiting interpretation"
              section={view.awaiting_interpretation}
              empty="No finished run or receipt is waiting for interpretation."
            >
              {(item) => (
                <>
                  <ResultDrafts drafts={item.drafts} />
                  <ResolveForm item={item} onDone={reload} />
                </>
              )}
            </NowGroup>
            ) : null}
            {view.running.count ? (
            <NowGroup
              id="now-running"
              title="Running (reported by the agent; never controlled here)"
              section={view.running}
              empty="Nothing is registered as running."
            />
            ) : null}
            {view.due.count ? (
              <NowGroup id="now-due" title="Due" section={view.due} empty="Nothing is due soon." />
            ) : null}
            {flagged.count ? (
            <NowGroup
              id="now-changed"
              title="Needs review after a change"
              section={flagged}
              empty="No record is flagged for review."
            />
            ) : null}
            <CheckedByAgent items={splitChanged(view.changed).checked} total={splitChanged(view.changed).checkedTotal} />
            <Executions />
          </>
          );
        }}
      </QueryView>
      {blockers.length > 0 ? (
        <details className="details now-group" data-testid="now-blockers">
          <summary>Capabilities not available ({blockers.length})</summary>
          <ul className="plain-list details-body">
            {blockers.map((f) => (
              <li key={f.feature} className="row" data-feature={f.feature}>
                <strong>{f.description}:</strong> {f.blocker?.message} <span className="muted">({f.blocker?.code})</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <p className="now-footer">
        <Link to="/artifacts" className="button secondary small">Artifacts: availability and backup coverage</Link>
      </p>
    </section>
  );
}

const OUTCOME_WORDS: Record<string, string> = {
  supports: "Supports its claim",
  challenges: "Challenges its claim",
  inconclusive: "Inconclusive",
};

/** Results an agent checked and the owner has not reviewed: visible, collapsed, in plain words;
 * a challenge is said first because it may change a claim (decision 0078). */
function CheckedByAgent({ items, total }: { items: NowItem[]; total: number }) {
  if (total === 0) return null;
  const challenged = items.filter((i) => (i.outcomes ?? []).some((o) => o !== "supports")).length;
  return (
    <details className="details now-group" data-testid="now-checked-by-agent" id="now-checked-by-agent">
      <summary>
        Checked by your agent, not yet by you ({total})
        {challenged ? <span className="muted"> · {challenged} challenge or leave open a claim</span> : null}
      </summary>
      <ul className="plain-list details-body">
        {items.length < total ? <li className="muted">Showing {items.length} of {total}.</li> : null}
        {items.map((item) => {
          const to = recordPath({ id: item.id, kind: item.kind, body: {} });
          const outcome = (item.outcomes ?? []).map((o) => OUTCOME_WORDS[o ?? ""] ?? "No outcome").join(" · ");
          return (
            <li key={item.id} className="row now-item" data-testid="now-checked-item" data-id={item.id}>
              {to ? <Link to={to}>{item.title}</Link> : <span>{item.title}</span>}{" "}
              <span className="badge">{outcome}</span>
              <p className="reason muted">{humanizeIds(item.reason)}</p>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

// Every registered run with what was reported and what a reader should assume now, including
// failed, cancelled and already interpreted ones (review F8).
function Executions() {
  const executions = useQuery<ExecutionList>("/executions?limit=100");
  const items = executions.data?.executions ?? [];
  return (
    <details className="details now-group" data-testid="now-executions">
      <summary>All registered runs ({items.length})</summary>
      <div className="details-body">
        <QueryView
          query={executions}
          loadingLabel="Loading runs"
          isEmpty={(d) => d.executions.length === 0}
          empty={<>No external run is registered.</>}
        >
          {(data) => (
            <ul className="plain-list">
              {data.executions.map((run) => (
                <li
                  key={run.id}
                  className="row"
                  data-testid="execution-row"
                  data-id={run.id}
                  data-state={run.effective_state}
                >
                  <strong>{run.title}</strong> <StateBadge state={run.effective_state} prefix="Run state" />
                  {run.state !== run.effective_state ? (
                    <span className="muted"> (reported {run.state.replace(/_/g, " ")}; silent since {run.silent_since})</span>
                  ) : null}
                  <p className="muted">
                    {run.reporter} on {run.host_label}
                    {run.last_reported_at ? `, last reported ${run.last_reported_at}` : ", no report yet"};{" "}
                    {run.receipts.length} receipt(s).{" "}
                    {run.awaiting_interpretation
                      ? "Awaiting interpretation."
                      : run.interpreted_by.length > 0
                        ? `Interpreted by ${run.interpreted_by
                            .map((c) => (c.method ? `${c.method} assessment` : "a reviewed entry"))
                            .join(", ")}.`
                        : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </QueryView>
      </div>
    </details>
  );
}
