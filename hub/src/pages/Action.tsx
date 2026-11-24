// Action detail (contextual page): plain statement and state first; why/evidence and IDs
// on demand. A05, CP18.1: every action is a work item, so this page also shows its
// description (the researcher's own text, shown as text) and labels, edits it (revise) and
// moves it through the existing action transitions. A protected step (adoption, done
// without evidence) goes through the owner-decision review.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, describeError, post } from "../api";
import { DecisionReview } from "../components/DecisionReview";
import {
  Details,
  KeyValues,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  formatDate,
} from "../components/ui";
import { useKindList, useRecords } from "../data";
import { ownerZone } from "../deadline";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { ActionBody, CommitResult, Envelope, ItemBody, SourceBody } from "../types";
import {
  Deadline,
  Labels,
  ProblemText,
  WorkFields,
  draftFields,
  draftFrom,
  draftProblem,
  freshRequest,
  type WorkDraft,
} from "./Work";

export function ActionPage({ id }: { id: string }) {
  const action = useQuery<Envelope<ActionBody>>(`/records/${encodeURIComponent(id)}`);
  return (
    <>
      <PageHeading title="Work item" />
      <QueryView query={action} loadingLabel="Loading work item">
        {/* Keyed by ID only: a newer revision (an agent, another window) must not reset an
            open edit form and lose the owner's draft (review s1-a F3). */}
        {(a) => <ActionView key={a.id} action={a} />}
      </QueryView>
    </>
  );
}

function ActionView({ action }: { action: Envelope<ActionBody> }) {
  const { project } = useHub();
  const zone = ownerZone(project);
  const [editing, setEditing] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);
  useEffect(() => {
    // After Save or Cancel, focus goes back to the Edit button instead of the document.
    if (!editing && returnFocus.current) {
      returnFocus.current = false;
      editButton.current?.focus();
    }
  }, [editing]);
  const refs = action.body.source_refs.map((r) => r.id);
  const sourcesRecs = useRecords(refs);
  const sources = useKindList<SourceBody>("source");
  const title = (sid: string) => sources.data?.items.find((s) => s.id === sid)?.body.title ?? "Source";
  const b = action.body;
  return (
    <article className="card" data-testid="work-item-detail" data-status={b.status}>
      <div className="card-head">
        <h2 className="card-title">{b.title}</h2>
        <StateBadge state={b.status} prefix="Work item state" />
      </div>
      <p className="card-meta">
        {b.owner_ref ? "Owner recorded" : "No owner recorded"} · <Deadline due={b.due_at} zone={zone} status={b.status} />
      </p>
      <Labels labels={b.labels} />
      <p>
        <strong>Why:</strong> {b.reason}
      </p>
      {b.blocker ? (
        <p>
          <strong>Blocked:</strong> {b.blocker}
        </p>
      ) : null}
      {b.description ? (
        <section aria-labelledby="work-description">
          <h3 className="subhead" id="work-description">
            What is wanted <span className="muted">(as written)</span>
          </h3>
          <p className="untrusted-text" data-testid="work-description">
            {b.description}
          </p>
        </section>
      ) : null}
      {editing ? (
        <EditWorkItem
          action={action}
          zone={zone}
          onDone={() => {
            returnFocus.current = true;
            setEditing(false);
          }}
        />
      ) : (
        <div className="button-row">
          <button type="button" className="button secondary" onClick={() => setEditing(true)} ref={editButton}>
            Edit
          </button>
        </div>
      )}
      <StatusControls action={action} />
      {refs.length ? (
        <>
          <h3 className="subhead">Evidence</h3>
          <ul className="plain-list">
            {refs.map((rid) => {
              const r = sourcesRecs.records.get(rid) as Envelope<ItemBody> | undefined;
              if (!r) return <li key={rid}>Linked record</li>;
              return (
                <li key={rid}>
                  Meeting {r.body.type}: “{r.body.text}”{" "}
                  {r.body.meeting_id ? <Link to={`/meetings/${r.body.meeting_id}`}>Open meeting</Link> : null}
                  <ul className="inline-list">
                    {r.body.evidence.map((ev, i) => (
                      <li key={i}>
                        <SourceButton
                          spec={{
                            versionId: ev.version_id,
                            title: title(ev.source_id),
                            startLine: ev.locator.start_line,
                            endLine: ev.locator.end_line,
                            startMs: ev.locator.start_ms as number | undefined,
                            endMs: ev.locator.end_ms as number | undefined,
                            citedBy: r.body.text,
                          }}
                        />
                      </li>
                    ))}
                  </ul>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {b.completion_evidence.length ? (
        <p className="muted">Completed with {b.completion_evidence.length} cited source(s).</p>
      ) : null}
      <Details>
        <KeyValues
          rows={[
            ["Action", <code key="a">{action.id}</code>],
            ["Revision", String(action.rev)],
            ["Recorded", formatDate(action.recorded_at)],
            ["Deadline (UTC)", b.due_at ?? "none"],
          ]}
        />
      </Details>
    </article>
  );
}

function EditWorkItem({ action, zone, onDone }: { action: Envelope<ActionBody>; zone: string; onDone: () => void }) {
  const { announce, refresh } = useHub();
  // The version the owner is editing. A save revises exactly this revision, so a newer one
  // (an agent, another window) makes the save stale instead of being overwritten.
  const [opened, setOpened] = useState(action);
  const [draft, setDraft] = useState<WorkDraft>(() => draftFrom(action.body, zone));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const problem = draftProblem(draft);
  const newer = action.rev !== opened.rev;

  useEffect(() => {
    heading.current?.focus();
  }, []);

  const reload = () => {
    setOpened(action);
    setDraft(draftFrom(action.body, zone));
    setError(null);
    announce("Latest version loaded; your earlier edits were discarded.");
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      const request = await freshRequest([
        {
          op: "revise",
          id: opened.id,
          base_rev: opened.rev,
          sensitivity: opened.sensitivity,
          occurred_at: opened.occurred_at,
          body: { ...opened.body, ...draftFields(draft, zone, opened.body.due_at) },
        },
      ]);
      await post<CommitResult>("/transactions", request);
      announce("Work item saved.");
      onDone();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const stale = error instanceof ApiError && error.kind === "stale";
  return (
    <form className="capture-form" onSubmit={save} aria-labelledby="edit-work-title" noValidate data-testid="edit-work-item">
      <h3 id="edit-work-title" className="subhead" tabIndex={-1} ref={heading}>
        Edit work item
      </h3>
      {newer || stale ? (
        <div className="stale-notice" role="alert" data-testid="work-edit-stale">
          <p className="state-title">Changed since you opened it</p>
          <p>
            This work item has a newer version (an agent or another window changed it).
            {stale ? " Nothing was saved." : ""} Saving these edits would be refused. Your edits are
            kept below; copy anything you need, then load the latest version.
          </p>
          <button type="button" className="button secondary" onClick={reload}>
            Load the latest version (discards these edits)
          </button>
        </div>
      ) : null}
      <WorkFields draft={draft} onChange={setDraft} zone={zone} idPrefix="work-edit" problem={problem} />
      <ProblemText problem={problem} idPrefix="work-edit" show />
      {error && !stale ? (
        <div className="state state-error" role="alert">
          <p className="state-title">{describeError(error).title}</p>
          <p>{describeError(error).detail} Your edits are kept.</p>
        </div>
      ) : null}
      <div className="button-row">
        <button type="submit" className="button primary" disabled={busy || problem !== null} aria-busy={busy}>
          {busy ? "Saving…" : "Save changes"}
        </button>
        <button type="button" className="button secondary" onClick={onDone} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

type Move = { to: string; label: string; needsReason?: string };

const MOVES: Record<string, Move[]> = {
  proposed: [{ to: "cancelled", label: "Cancel" }],
  ready: [
    { to: "in_progress", label: "Start" },
    { to: "blocked", label: "Mark blocked", needsReason: "What is it waiting for?" },
    { to: "cancelled", label: "Cancel" },
  ],
  in_progress: [
    { to: "ready", label: "Back to ready" },
    { to: "blocked", label: "Mark blocked", needsReason: "What is it waiting for?" },
    { to: "cancelled", label: "Cancel" },
  ],
  blocked: [
    { to: "ready", label: "Unblock" },
    { to: "cancelled", label: "Cancel" },
  ],
  done: [{ to: "ready", label: "Reopen", needsReason: "Why reopen it?" }],
  cancelled: [{ to: "proposed", label: "Restore", needsReason: "Why restore it?" }],
};

function StatusControls({ action }: { action: Envelope<ActionBody> }) {
  const { announce, refresh } = useHub();
  const status = action.body.status;
  const [asking, setAsking] = useState<Move | null>(null);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const transition = (to: string, why: string | null) => ({
    op: "transition",
    id: action.id,
    base_rev: action.rev,
    to,
    reason: why,
    evidence: [],
  });

  const move = async (m: Move, why: string | null) => {
    setBusy(true);
    setError(null);
    try {
      await post<CommitResult>("/transactions", await freshRequest([transition(m.to, why)]));
      setAsking(null);
      setReason("");
      announce(`Work item: ${m.label.toLowerCase()} done.`);
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const submitReason = (event: FormEvent) => {
    event.preventDefault();
    if (asking && reason.trim()) void move(asking, reason.trim());
  };

  return (
    <section className="status-controls" aria-label="Change state" data-testid="work-status-controls">
      <div className="button-row">
        {(MOVES[status] ?? []).map((m) => (
          <button
            key={m.to}
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => (m.needsReason ? setAsking(m) : void move(m, null))}
          >
            {m.label}
          </button>
        ))}
      </div>
      {asking ? (
        <form onSubmit={submitReason} className="inline-form">
          <div className="field">
            <label htmlFor="work-move-reason">{asking.needsReason}</label>
            <input id="work-move-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
          </div>
          <div className="button-row">
            <button type="submit" className="button primary" disabled={busy || !reason.trim()}>
              {asking.label}
            </button>
            <button type="button" className="button secondary" onClick={() => setAsking(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      {status === "proposed" ? (
        <DecisionReview
          startLabel="Adopt as a work item"
          title="adopt this work item"
          approveLabel="Adopt"
          affected={<p>Marks “{action.body.title}” ready: it becomes one of your open work items.</p>}
          build={() => freshRequest([transition("ready", "Adopted by the researcher in the hub")])}
          onApplied={() => announce("Work item adopted.")}
        />
      ) : null}
      {status === "in_progress" ? (
        <div className="field">
          <label htmlFor="work-done-note">
            Done? Say what was done <span className="muted">(an agent links evidence instead)</span>
          </label>
          <input id="work-done-note" value={note} onChange={(e) => setNote(e.target.value)} />
          <DecisionReview
            startLabel="Mark done"
            title="mark this work item done on your note"
            approveLabel="Mark done"
            disabled={!note.trim()}
            affected={<p>Marks “{action.body.title}” done with your note and no linked evidence.</p>}
            build={() => freshRequest([transition("done", note.trim())])}
            onApplied={() => {
              setNote("");
              announce("Work item marked done.");
            }}
          />
        </div>
      ) : null}
      {error ? (
        <div className="state state-error" role="alert">
          <p className="state-title">{describeError(error).title}</p>
          <p>{describeError(error).detail}</p>
        </div>
      ) : null}
    </section>
  );
}
