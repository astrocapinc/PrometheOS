// A05, CP18.2-CP18.3: the event inbox on Now (decision 0035). Unacknowledged research events,
// each with its reason and a time in the owner's zone. The owner acknowledges one (optionally
// with a note, for example why it can be dismissed) or all shown. An event is a fact:
// acknowledging it records that the owner has seen it and changes nothing else.
import { useEffect, useState } from "react";
import { describeError, post } from "../api";
import { ErrorState, QueryView } from "../components/ui";
import { recordPath } from "../data";
import { formatDeadline, ownerZone } from "../deadline";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { AcknowledgeResult, EventList, ResearchEvent, TickResult } from "../types";

const POLL_MS = 15000;

const TYPE_LABELS: Record<string, string> = {
  "work_item.created": "Work item added",
  "work_item.changed": "Work item changed",
  "deadline.approaching": "Deadline approaching",
  "approval.requested": "Approval requested",
  "approval.decided": "Approval decided",
  "result.awaiting_interpretation": "Result awaiting interpretation",
  "correction.flagged_dependents": "Correction flagged records",
  "backup.coverage_gap": "Backup gap",
  "source.unavailable": "Source unavailable",
  "run.outcome_unknown": "Operation outcome unknown",
  "claim.needs_review": "A claim is stale or conflicting: do not quote it",
};

export function eventLabel(type: string): string {
  return TYPE_LABELS[type] ?? type;
}

function subjectLink(event: ResearchEvent): { to: string; label: string } | null {
  const { kind, id } = event.subject_ref;
  switch (kind) {
    case "approval_request":
      return { to: `/approvals/${id}`, label: "Open the request" };
    case "operation_run":
      return { to: `/runs/${id}`, label: "Open the operation" };
    case "project":
    case "source":
      return { to: "/artifacts", label: "Open artifacts and backup coverage" };
    case "execution":
    case "version":
      return null; // shown under "Results awaiting interpretation" on this page
    default: {
      const to = recordPath({ id, kind, body: {} });
      return to ? { to, label: kind === "action" ? "Open the work item" : "Open it" } : null;
    }
  }
}

function EventRow({
  event,
  zone,
  busy,
  onAcknowledge,
}: {
  event: ResearchEvent;
  zone: string;
  busy: boolean;
  onAcknowledge: (ids: string[], note: string | null) => Promise<boolean>;
}) {
  const [note, setNote] = useState("");
  const link = subjectLink(event);
  const due = typeof event.data.due_at === "string" ? event.data.due_at : null;
  return (
    <li
      className="row now-item"
      data-testid="inbox-event"
      data-type={event.type}
      data-event-id={event.event_id}
      data-subject={event.subject_ref.id}
    >
      <div className="event-text">
      <span className="badge badge-kind">{eventLabel(event.type)}</span>{" "}
      {event.subject_title ? (
        <>
          <strong data-testid="inbox-subject">{event.subject_title}</strong>:{" "}
        </>
      ) : null}
      <span data-testid="inbox-reason">{event.reason}</span>
      <p className="muted small-print" data-testid="inbox-when">
        {due ? (
          <>
            Deadline {formatDeadline(due, zone)} ·{" "}
          </>
        ) : null}
        Noted {formatDeadline(event.occurred_at, zone)}
        {link ? (
          <>
            {" "}
            · <Link to={link.to}>{link.label}</Link>
          </>
        ) : null}
      </p>
      </div>
      <form
        className="ack-form"
        onSubmit={(e) => {
          e.preventDefault();
          // The note stays when the acknowledgement fails, so nothing typed is lost.
          void onAcknowledge([event.event_id], note.trim() || null).then((ok) => ok && setNote(""));
        }}
      >
        <label className="visually-hidden" htmlFor={`ack-note-${event.event_id}`}>
          Note (optional)
        </label>
        <input
          id={`ack-note-${event.event_id}`}
          data-testid="inbox-note"
          type="text"
          maxLength={2000}
          placeholder="Note (optional), for example why it can be dismissed"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <button
          type="submit"
          className="button secondary small"
          disabled={busy}
          data-testid="inbox-ack"
          aria-label={`Acknowledge: ${eventLabel(event.type)}${event.subject_title ? `, ${event.subject_title}` : ""}`}
        >
          Acknowledge
        </button>
      </form>
    </li>
  );
}

// The queue's order: entries with a deadline first (nearest first), then by age.
function byDueThenAge(items: ResearchEvent[]): ResearchEvent[] {
  const due = (e: ResearchEvent) => (typeof e.data.due_at === "string" ? e.data.due_at : null);
  return [...items].sort((a, b) => {
    const da = due(a);
    const db = due(b);
    if (da !== db) return da === null ? 1 : db === null ? -1 : da < db ? -1 : 1;
    if (a.occurred_at !== b.occurred_at) return a.occurred_at < b.occurred_at ? -1 : 1;
    return a.event_id < b.event_id ? -1 : 1;
  });
}

export function EventInbox() {
  const { project, announce } = useHub();
  const zone = ownerZone(project);
  const events = useQuery<EventList>("/events?state=open&limit=200");
  const reload = events.reload;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const acknowledge = async (ids: string[], note: string | null): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      const done = await post<AcknowledgeResult>("/events/acknowledge", { event_ids: ids, note });
      const n = done.acknowledged.length;
      announce(n === 1 ? "Event acknowledged." : `${n} events acknowledged.`);
      reload();
      return true;
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setBusy(true);
    setError(null);
    try {
      const tick = await post<TickResult>("/jobs/tick", {});
      const n = tick.events.emitted.length;
      announce(n === 0 ? "Checks ran; no new events." : n === 1 ? "Checks ran; 1 new event." : `Checks ran; ${n} new events.`);
      reload();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="inbox" className="section" data-testid="event-inbox">
      <h2 id="inbox">Inbox</h2>
      <QueryView
        query={events}
        loadingLabel="Loading events"
        isEmpty={(d) => d.items.length === 0}
        empty={
          <>
            No unacknowledged events. Approaching deadlines, approval requests, results awaiting
            interpretation and backup gaps appear here when they need attention.
          </>
        }
      >
        {(data) => (
          <>
            <ul className="plain-list" data-testid="inbox-events">
              {byDueThenAge(data.items).map((event) => (
                <EventRow
                  key={event.event_id}
                  event={event}
                  zone={zone}
                  busy={busy}
                  onAcknowledge={acknowledge}
                />
              ))}
            </ul>
            {data.items.length > 1 ? (
              <div className="button-row">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  data-testid="inbox-ack-all"
                  onClick={() => void acknowledge(byDueThenAge(data.items).slice(0, 50).map((e) => e.event_id), null)}
                >
                  Acknowledge all shown{data.items.length > 50 ? " (first 50)" : ""}
                </button>
              </div>
            ) : null}
          </>
        )}
      </QueryView>
      {error ? <ErrorState error={error} /> : null}
      <div className="button-row">
        <button type="button" className="button secondary small" disabled={busy} data-testid="inbox-check" onClick={() => void check()}>
          Run due checks now
        </button>
      </div>
      <details className="muted small-print">
        <summary>About events</summary>
        Events are facts. Acknowledging one only records that you have seen it. Agents see the
        unacknowledged ones in their queue when you open a session, and ask before doing anything.
        New events are found when the hub or an agent session starts and when checks run ("Run
        due checks now" also runs any recurring checks that are due). Your notes are visible to
        agents through the event list.
      </details>
    </section>
  );
}
