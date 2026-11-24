// Work items (A05, CP18.1): tasks and assignments the researcher adds, with an optional
// deadline, a description of what is wanted and labels. A work item is an ordinary action:
// adding one creates it as proposed and adopts it (proposed -> ready, a protected owner
// decision reviewed before it applies); editing is a revise; status changes are the existing
// action transitions. Coding agents see the open ones and ask before working on any.
import { useMemo, useState, type ReactNode } from "react";
import { get, uuid4 } from "../api";
import { DecisionReview } from "../components/DecisionReview";
import { Details, Empty, PageHeading, QueryView, StateBadge } from "../components/ui";
import { useKindList } from "../data";
import {
  dueNote,
  formatDeadline,
  labelProblem,
  ownerZone,
  parseLabels,
  sameWallTime,
  utcToZoned,
  zonedToUtc,
} from "../deadline";
import { useHandoff } from "../handoff";
import { foldSuggestions } from "../fold";
import { useHub } from "../hub";
import { Link } from "../router";
import type { ActionBody, Envelope, ProjectInfo } from "../types";

const OPEN = new Set(["ready", "in_progress", "blocked"]);
export const MAX_DESCRIPTION = 20_000;

/** The editable fields of a work item, as the form holds them. */
export interface WorkDraft {
  title: string;
  reason: string;
  date: string; // "" = no deadline
  time: string;
  description: string;
  labels: string;
}

export function emptyDraft(): WorkDraft {
  return { title: "", reason: "", date: "", time: "23:59", description: "", labels: "" };
}

export function draftFrom(body: ActionBody, zone: string): WorkDraft {
  const due = body.due_at ? utcToZoned(body.due_at, zone) : { date: "", time: "23:59" };
  return {
    title: body.title,
    reason: body.reason,
    date: due.date,
    time: due.time,
    description: body.description ?? "",
    labels: (body.labels ?? []).join(", "),
  };
}

export interface DraftProblem {
  field: "title" | "reason" | "time" | "description" | "labels";
  message: string;
}

/** What is wrong with the draft, and in which field, or null. */
export function draftProblem(d: WorkDraft): DraftProblem | null {
  if (!d.title.trim()) return { field: "title", message: "Give the work item a title." };
  if (d.title.trim().length > 300) return { field: "title", message: "Keep the title to 300 characters." };
  if (!d.reason.trim()) return { field: "reason", message: "Say briefly why it matters (one line is enough)." };
  if (d.date && !d.time) return { field: "time", message: "Give the deadline a time, or clear the date." };
  if (d.description.length > MAX_DESCRIPTION)
    return { field: "description", message: "Keep the description to 20,000 characters." };
  const labels = labelProblem(parseLabels(d.labels));
  return labels ? { field: "labels", message: labels } : null;
}

/** The body fields a draft sets; the rest of an existing body is kept. An existing deadline
 *  whose date and time were not touched is kept exactly (seconds, and which occurrence of a
 *  repeated hour), never re-derived from the minute shown in the form (review s1-a F4). */
export function draftFields(d: WorkDraft, zone: string, existingDue: string | null = null) {
  let due: string | null = null;
  if (d.date) {
    due =
      existingDue && sameWallTime(existingDue, d.date, d.time, zone) ? existingDue : zonedToUtc(d.date, d.time, zone);
  }
  return {
    title: d.title.trim(),
    reason: d.reason.trim(),
    due_at: due,
    description: d.description.trim() ? d.description : null,
    labels: parseLabels(d.labels),
  };
}

export async function freshRequest(operations: unknown[]) {
  const info = await get<ProjectInfo>("/project");
  return {
    schema_version: 1,
    project_id: info.project_id,
    request_id: uuid4(),
    expected_seq: info.project_seq,
    operations,
  };
}

/** The validation message under the form; fields point at it with aria-describedby. */
export function ProblemText({ problem, idPrefix, show }: { problem: DraftProblem | null; idPrefix: string; show: boolean }) {
  if (!problem || !show) return null;
  return (
    <p className="field-error" role="status" id={`${idPrefix}-problem`} data-testid="work-problem">
      {problem.message}
    </p>
  );
}

export function WorkFields({
  draft,
  onChange,
  zone,
  idPrefix,
  problem,
}: {
  draft: WorkDraft;
  onChange: (next: WorkDraft) => void;
  zone: string;
  idPrefix: string;
  /** The current problem, shown on its field (aria-invalid, aria-describedby). */
  problem?: DraftProblem | null;
}) {
  const set = (patch: Partial<WorkDraft>) => onChange({ ...draft, ...patch });
  const id = (name: string) => `${idPrefix}-${name}`;
  const invalid = (field: DraftProblem["field"]) =>
    problem?.field === field ? { "aria-invalid": true, "aria-describedby": `${idPrefix}-problem` } : {};
  return (
    <>
      <div className="field">
        <label htmlFor={id("title")}>Title</label>
        <input
          id={id("title")}
          value={draft.title}
          maxLength={300}
          onChange={(e) => set({ title: e.target.value })}
          placeholder="Submit paper P1 to journal X"
          required
          {...invalid("title")}
        />
      </div>
      <div className="field">
        <label htmlFor={id("reason")}>Why it matters</label>
        <input
          id={id("reason")}
          value={draft.reason}
          onChange={(e) => set({ reason: e.target.value })}
          placeholder="Agreed with my supervisor"
          required
          {...invalid("reason")}
        />
      </div>
      <fieldset className="deadline-fields">
        <legend>
          Deadline <span className="muted">(optional, {zone})</span>
        </legend>
        <div className="field-row">
          <div className="field">
            <label htmlFor={id("date")}>Date</label>
            <input id={id("date")} type="date" value={draft.date} onChange={(e) => set({ date: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor={id("time")}>Time</label>
            <input
              id={id("time")}
              type="time"
              value={draft.time}
              onChange={(e) => set({ time: e.target.value })}
              {...invalid("time")}
            />
          </div>
          {draft.date ? (
            <button type="button" className="button secondary small" onClick={() => set({ date: "" })}>
              No deadline
            </button>
          ) : null}
        </div>
      </fieldset>
      <div className="field">
        <label htmlFor={id("description")}>
          What is wanted <span className="muted">(optional)</span>
        </label>
        <textarea
          id={id("description")}
          value={draft.description}
          rows={5}
          onChange={(e) => set({ description: e.target.value })}
          {...invalid("description")}
        />
        <p className="muted small-print">
          Your agent reads this as your notes about the task, never as instructions it must follow.
        </p>
      </div>
      <div className="field">
        <label htmlFor={id("labels")}>
          Labels <span className="muted">(optional, separated by commas)</span>
        </label>
        <input
          id={id("labels")}
          value={draft.labels}
          onChange={(e) => set({ labels: e.target.value })}
          placeholder="paper P1, submission"
          {...invalid("labels")}
        />
      </div>
    </>
  );
}

export function Labels({ labels }: { labels?: string[] }) {
  if (!labels?.length) return null;
  return (
    <ul className="label-list" aria-label="Labels" data-testid="work-labels">
      {labels.map((l) => (
        <li key={l} className="badge badge-kind">
          {l}
        </li>
      ))}
    </ul>
  );
}

export function Deadline({ due, zone, status }: { due: string | null; zone: string; status?: string }) {
  if (!due) return <>No deadline</>;
  const note = dueNote(due, Date.now(), status);
  return (
    <span data-testid="work-deadline" data-due={due}>
      Due {formatDeadline(due, zone)}
      {note ? <> · {note}</> : null}
    </span>
  );
}

function WorkCard({ action, zone }: { action: Envelope<ActionBody>; zone: string }) {
  const b = action.body;
  return (
    <li className="card" data-testid="work-item" data-status={b.status}>
      <div className="card-head">
        <h3 className="card-title">
          <Link to={`/actions/${action.id}`}>{b.title}</Link>
        </h3>
        <StateBadge state={b.status} prefix="Work item state" />
      </div>
      <p className="card-meta">
        <Deadline due={b.due_at} zone={zone} status={b.status} />
        {b.blocker ? <> · Blocked: {b.blocker}</> : null}
        {b.description ? <> · Has a description ({b.description.length.toLocaleString()} characters)</> : null}
      </p>
      <Labels labels={b.labels} />
    </li>
  );
}

function byDeadline(a: Envelope<ActionBody>, b: Envelope<ActionBody>): number {
  const da = a.body.due_at ?? "9999";
  const db = b.body.due_at ?? "9999";
  return da === db ? (a.recorded_at < b.recorded_at ? -1 : 1) : da < db ? -1 : 1;
}

function List({ items, zone, testId }: { items: Envelope<ActionBody>[]; zone: string; testId: string }) {
  return (
    <ol className="card-list" data-testid={testId}>
      {items.map((a) => (
        <WorkCard key={a.id} action={a} zone={zone} />
      ))}
    </ol>
  );
}

function AddWorkItem({ zone }: { zone: string }) {
  const { announce } = useHub();
  const handed = useHandoff("work", (value) => setDraft((d) => ({ ...d, title: value })));
  const [draft, setDraft] = useState<WorkDraft>(() => ({ ...emptyDraft(), title: handed }));
  const [actionId, setActionId] = useState(() => `action_${uuid4()}`);
  const [added, setAdded] = useState<{ id: string; title: string } | null>(null);
  const problem = draftProblem(draft);
  // Only point out a problem once the owner has started typing.
  const shown = problem !== null && Boolean(draft.title || draft.reason);
  const fields = useMemo(() => (problem ? null : draftFields(draft, zone)), [draft, problem, zone]);

  const build = async () => {
    if (!fields) throw new Error(problem?.message ?? "The form is incomplete.");
    return freshRequest([
      {
        op: "create",
        kind: "action",
        id: actionId,
        base_rev: 0,
        sensitivity: "normal",
        occurred_at: null,
        body: {
          ...fields,
          source_refs: [],
          owner_ref: null,
          dependencies: [],
          status: "proposed",
          blocker: null,
          completion_evidence: [],
          commitment_item_id: null,
        },
      },
      { op: "transition", id: actionId, base_rev: 1, to: "ready", reason: "Added by the researcher in the hub", evidence: [] },
    ]);
  };

  const summary: ReactNode = fields ? (
    <ul>
      <li>
        Adds the work item “{fields.title}” and marks it ready.
      </li>
      <li>{fields.due_at ? <>Deadline: {formatDeadline(fields.due_at, zone)}.</> : "No deadline."}</li>
      {fields.labels.length ? <li>Labels: {fields.labels.join(", ")}.</li> : null}
      {fields.description ? <li>With a description ({fields.description.length.toLocaleString()} characters).</li> : null}
    </ul>
  ) : null;

  return (
    <section className="card capture-form" aria-labelledby="add-work-title" data-testid="add-work-item">
      <h2 id="add-work-title">Add a work item</h2>
      <WorkFields
        draft={draft}
        onChange={(d) => {
          setDraft(d);
          setAdded(null);
        }}
        zone={zone}
        idPrefix="work-new"
        problem={shown ? problem : null}
      />
      <ProblemText problem={problem} idPrefix="work-new" show={shown} />
      {added ? (
        <p className="success" data-testid="work-added">
          <span aria-hidden="true">✓ </span>Added “{added.title}”. <Link to={`/actions/${added.id}`}>Open it</Link>
        </p>
      ) : null}
      <DecisionReview
        startLabel="Review and add"
        title="add this work item"
        approveLabel="Add work item"
        affected={summary}
        build={build}
        disabled={problem !== null}
        onApplied={() => {
          setAdded({ id: actionId, title: fields?.title ?? "" });
          setDraft(emptyDraft());
          setActionId(`action_${uuid4()}`);
          announce("Work item added.");
        }}
      />
    </section>
  );
}

function Suggestions({ items, zone }: { items: Envelope<ActionBody>[]; zone: string }) {
  const { loose, folded } = foldSuggestions(items);
  loose.sort(byDeadline);
  return (
    <>
      {loose.length ? <List items={loose} zone={zone} testId="proposed-work" /> : null}
      {folded.map(([label, group]) => (
        <Details key={label ?? ""} label={`${label === null ? "Without a label" : `Labelled “${label}”`} (${group.length})`}>
          {label === "adoption" ? (
            <p className="muted">
              Steps of the adoption plan. You decide them on the <Link to="/adoption">Adoption</Link> page; adopting
              one here only puts it on your list.
            </p>
          ) : null}
          <List items={group.sort(byDeadline)} zone={zone} testId="proposed-work-group" />
        </Details>
      ))}
    </>
  );
}

export function WorkPage() {
  const { project } = useHub();
  const zone = ownerZone(project);
  const actions = useKindList<ActionBody>("action");
  return (
    <>
      <PageHeading
        title="Work items"
        lead={
          <>
            Tasks and assignments with optional deadlines. When you start a session, your coding agent
            lists the open ones and asks whether to work on one; it never starts one by itself. Times
            are shown in {zone}.
          </>
        }
      />
      <AddWorkItem zone={zone} />
      <QueryView query={actions} loadingLabel="Loading work items">
        {(data) => {
          const items = data.items;
          const open = items.filter((a) => OPEN.has(a.body.status)).sort(byDeadline);
          const proposed = items.filter((a) => a.body.status === "proposed").sort(byDeadline);
          const closed = items.filter((a) => a.body.status === "done" || a.body.status === "cancelled");
          return (
            <>
              <section className="section" aria-labelledby="open-work">
                <h2 id="open-work">Open ({open.length})</h2>
                {open.length ? <List items={open} zone={zone} testId="open-work" /> : <Empty>No open work items. Add one above, or press Ctrl K anywhere and choose &quot;Add a work item&quot;.</Empty>}
              </section>
              {proposed.length ? (
                <section className="section" aria-labelledby="proposed-work">
                  <h2 id="proposed-work">Suggested, waiting for you ({proposed.length})</h2>
                  <p className="muted">Proposed by an agent or from a meeting. Open one to adopt or cancel it.</p>
                  <Suggestions items={proposed} zone={zone} />
                </section>
              ) : null}
              {closed.length ? (
                <Details label={`Done or cancelled (${closed.length})`}>
                  <List items={closed.sort(byDeadline)} zone={zone} testId="closed-work" />
                </Details>
              ) : null}
            </>
          );
        }}
      </QueryView>
    </>
  );
}
