// Small shared building blocks: states, badges, disclosure and source links.
// All record text is rendered as plain React text nodes (never as HTML or Markdown).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, describeError } from "../api";
import { useHub, type QueryState, type SourceSpec } from "../hub";
import { Icon } from "./icons";

export function PageHeading({ title, lead }: { title: string; lead?: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = `${title} · PrometheOS hub`;
  }, [title]);
  return (
    <div className="page-heading">
      <h1 id="page-title" tabIndex={-1} ref={ref}>
        {title}
      </h1>
      {lead ? <p className="lead">{lead}</p> : null}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state state-loading" role="status">
      <p className="state-loading-label">
        <span className="spinner" aria-hidden="true" /> {label}…
      </p>
      <span className="skeleton" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
    </div>
  );
}

/** An empty list that says what will appear here and, where one exists, the action that fills it. */
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="state state-empty">
      <Icon name="inbox" size={18} className="state-empty-icon" />
      <p>{children}</p>
    </div>
  );
}

/** Copy a command to the clipboard; the text stays selectable when the clipboard is refused. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const { announce } = useHub();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      className="icon-button copy-button"
      aria-label={label}
      title={copied ? "Copied" : label}
      onClick={async () => {
        try {
          if (!navigator.clipboard) throw new Error("no clipboard");
          await navigator.clipboard.writeText(text);
          setCopied(true);
          announce("Copied to the clipboard.");
        } catch {
          announce("The clipboard is not available; select the command and copy it.");
        }
      }}
    >
      <Icon name={copied ? "check" : "copy"} />
    </button>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, detail } = describeError(error);
  const kind = error instanceof ApiError ? error.kind : "server";
  return (
    <div className={`state state-error state-${kind}`} role="alert" data-error-kind={kind}>
      <p className="state-title">
        <span className="state-icon" aria-hidden="true">
          {kind === "offline" ? "⚠" : "!"}
        </span>{" "}
        {title}
      </p>
      <p>{detail}</p>
      {onRetry && kind !== "unpaired" ? (
        <button type="button" className="button secondary" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

/** Render loading / error / empty / data for one query, consistently on every route. */
export function QueryView<T>({
  query,
  isEmpty,
  empty,
  loadingLabel,
  children,
}: {
  query: QueryState<T>;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  loadingLabel?: string;
  children: (data: T) => ReactNode;
}) {
  const { data, error, loading, reload } = query;
  if (data === undefined) {
    if (error) return <ErrorState error={error} onRetry={reload} />;
    if (loading) return <Loading label={loadingLabel} />;
    return null;
  }
  return (
    <>
      {error ? (
        <div className="inline-warning" role="status">
          Showing the last loaded version. {describeError(error).title}.{" "}
          <button type="button" className="link-button" onClick={reload}>
            Retry
          </button>
        </div>
      ) : null}
      {isEmpty && isEmpty(data) ? <Empty>{empty}</Empty> : children(data)}
    </>
  );
}

const STATE_LABELS: Record<string, string> = {
  proposed: "Proposed",
  adopted: "Adopted",
  dismissed: "Dismissed",
  superseded: "Superseded",
  ready: "Ready",
  in_progress: "In progress",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
  applied: "Applied",
  rejected: "Rejected",
  supported_within_conditions: "Supported within conditions",
  inconclusive: "Inconclusive",
  contested: "Contested",
  withdrawn: "Withdrawn",
  reviewed: "Reviewed",
  unreviewed: "Not reviewed",
  retained: "Retained",
  available: "Available",
  installed: "Installed",
  quarantined: "Quarantined",
  removed: "Removed",
  captured: "Captured",
  planned: "Planned",
  active: "Active",
  completed: "Completed",
  pending: "Pending",
  skipped: "Skipped",
  planning: "Planning",
  drafting: "Drafting",
  internal_review: "Internal review",
  ready_for_submission: "Ready for submission",
  submitted: "Submitted (recorded)",
  revision: "Revision",
  accepted: "Accepted (recorded)",
  current: "Current",
  needs_review: "Needs review",
  resolved: "Resolved",
  missing: "Missing",
  external: "Referenced only",
  reference_only: "Reference only",
  unavailable: "Unavailable",
  backed_up: "Backed up",
  not_backed_up: "Not backed up",
  registered: "Registered",
  reported_running: "Reported running",
  reported_completed: "Reported completed",
  reported_failed: "Reported failed",
  reported_cancelled: "Reported cancelled",
  status_unknown: "Status unknown",
};

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state.replace(/_/g, " ");
}

/** A state shown as text (never colour alone). */
export function StateBadge({ state, prefix }: { state: string; prefix?: string }) {
  return (
    <span className={`badge badge-${state}`} data-state={state}>
      {prefix ? <span className="visually-hidden">{prefix}: </span> : null}
      {stateLabel(state)}
    </span>
  );
}

/** A run's reported progress: a native bar (indeterminate without a total) and the words. */
export function Progress({
  done,
  total,
  unit,
  label,
  active,
}: {
  done: number;
  total: number | null;
  unit: string;
  label: string;
  /** Still working: without a total the bar is indeterminate; a stopped run shows only text. */
  active: boolean;
}) {
  const text = total ? `${done} of ${total} ${unit}` : `${done} ${unit}`;
  return (
    <span className="progress-line">
      {total ? (
        <progress className="progress" max={total} value={Math.min(done, total)} aria-label={label} />
      ) : active ? (
        <progress className="progress" aria-label={label} />
      ) : null}
      <span>{text}</span>
    </span>
  );
}

export function Details({ children, label = "Details" }: { children: ReactNode; label?: string }) {
  return (
    <details className="details">
      <summary>{label}</summary>
      <div className="details-body">{children}</div>
    </details>
  );
}

export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([key, value]) => (
        <div key={key} className="kv-row">
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function lineLabel(start?: number, end?: number): string {
  if (start === undefined) return "whole source";
  if (end === undefined || end === start) return `line ${start}`;
  return `lines ${start}–${end}`;
}

/** m:ss (or h:mm:ss) from milliseconds, for transcript times. */
export function clock(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return `${h > 0 ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

export function spanLabel(spec: {
  startLine?: number;
  endLine?: number;
  startMs?: number;
  endMs?: number;
  locatorLabel?: string;
}): string {
  // Dogfood F5: a receipt citation reads as the artifact it names, never as /outputs/0.
  if (spec.locatorLabel) return spec.locatorLabel;
  if (spec.startMs !== undefined) {
    return `transcript ${clock(spec.startMs)}–${clock(spec.endMs ?? spec.startMs)}`;
  }
  return lineLabel(spec.startLine, spec.endLine);
}

export function SourceButton({
  spec,
  children,
  className = "source-button",
}: {
  spec: SourceSpec;
  children?: ReactNode;
  className?: string;
}) {
  const { openSource } = useHub();
  return (
    <button
      type="button"
      className={className}
      onClick={(event) => openSource(spec, event.currentTarget)}
      aria-label={`Open source ${spec.title}, ${spanLabel(spec)}`}
    >
      {children ?? (
        <>
          <span aria-hidden="true">¶ </span>
          {spec.title} · {spanLabel(spec)}
        </>
      )}
    </button>
  );
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "date not recorded";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDay(iso: string | null | undefined): string {
  if (!iso) return "Undated";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

export function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    entry: "Notebook entry",
    claim: "Claim",
    correction: "Correction",
    item: "Meeting item",
    action: "Action",
    meeting: "Meeting",
    source: "Source",
    version: "Source version",
    project: "Project",
    task: "Agent task",
    person: "Person",
    package: "Skill package",
    run: "Workflow run",
    assessment: "Assessment",
    paper: "Paper",
    anchor: "Manuscript anchor",
    job: "Recurring check",
    execution: "External run",
    operation_run: "App operation",
    approval_request: "Approval request",
    work: "Library work",
  };
  return labels[kind] ?? kind;
}

export function kindOf(id: string): string {
  return id.split("_", 1)[0] ?? id;
}

const RECORD_ID = /'?\b([a-z]+)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'?/g;

/** Replace raw record IDs inside server-written sentences with plain words
 *  (the IDs stay available behind Details). */
export function humanizeIds(text: string): string {
  return text.replace(RECORD_ID, (_m, kind: string) => {
    const label = kindLabel(kind).toLowerCase();
    return `${/^[aeiou]/.test(label) ? "an" : "a"} ${label}`;
  });
}
