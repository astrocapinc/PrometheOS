// Meeting review: agent-proposed items beside the exact original they cite (typed notes lines or
// a recording's transcript with its audio). Each item shows how it relates to current actions;
// similarity only proposes, so the researcher chooses explicitly: adopt as a new action, treat it
// as already covered by an existing one (recorded as a dismissal with its reason), or dismiss.
// Adopting a commitment is one owner decision that creates exactly one action
// (create action -> ready, item -> adopted). Questions stay open and unowned.
import { useMemo, useRef, useState } from "react";
import { describeError, get, post, uuid4 } from "../api";
import { AudioPlayer, TranscriptList, useSpanPlayer } from "../components/audio";
import { DecisionReview } from "../components/DecisionReview";
import {
  Details,
  Empty,
  ErrorState,
  KeyValues,
  Loading,
  PageHeading,
  QueryView,
  StateBadge,
  clock,
  formatDate,
  lineLabel,
  spanLabel,
} from "../components/ui";
import { recordTitle, useRecords } from "../data";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type {
  CommitResult,
  Envelope,
  ItemBody,
  MeetingBody,
  ProjectInfo,
  Reconcile,
  ReconcileItem,
  RelatedAction,
  TranscriptView,
  VersionBody,
  VersionText,
} from "../types";
import { ProviderRunPanel } from "./Provider";

const ADOPTABLE = new Set(["commitment", "suggestion", "request", "decision"]);

const TYPE_LABELS: Record<string, string> = {
  commitment: "Commitment",
  suggestion: "Suggestion",
  question: "Question",
  request: "Request",
  decision: "Decision",
  observation: "Observation",
};

const CLASS_TEXT: Record<string, string> = {
  new: "No similar action found. It would be new work.",
  possibly_already_done: "A similar action is already marked done. This may already be addressed.",
  possibly_overlaps_open_action: "A similar action is open. This may overlap work already planned.",
  adopted: "Adopted: it became an action.",
  dismissed: "Dismissed.",
  superseded: "Superseded by a later item.",
};

interface Selection {
  itemId: string;
  start?: number;
  end?: number;
  startMs?: number;
  endMs?: number;
}

function evidenceSpan(item: ReconcileItem): Selection {
  const ev = item.evidence[0];
  if (!ev) return { itemId: item.item_id };
  if (ev.locator.type === "audio") {
    return {
      itemId: item.item_id,
      startMs: ev.locator.start_ms as number,
      endMs: ev.locator.end_ms as number,
    };
  }
  return { itemId: item.item_id, start: ev.locator.start_line, end: ev.locator.end_line };
}

export function MeetingPage({ id }: { id: string }) {
  const meeting = useQuery<Envelope<MeetingBody>>(`/records/${encodeURIComponent(id)}`);
  const reconcile = useQuery<Reconcile>(`/meetings/${encodeURIComponent(id)}/reconcile`);
  const [selected, setSelected] = useState<Selection | null>(null);
  const notesRef = useRef<HTMLDivElement>(null);
  const versionId = meeting.data?.body.source_version_refs[0]?.id ?? null;
  const version = useQuery<Envelope<VersionBody>>(versionId ? `/records/${encodeURIComponent(versionId)}` : null);
  const isTranscript = version.data?.body.payload_schema === "transcript_v1";
  const relatedIds = useMemo(
    () => [...new Set((reconcile.data?.items ?? []).flatMap((i) => i.related_actions.map((r) => r.action_id)))],
    [reconcile.data],
  );
  const actions = useRecords(relatedIds);

  const select = (item: ReconcileItem) => {
    setSelected(evidenceSpan(item));
    // Bring the original pane to the cited span (stacked layouts: scroll the pane into view).
    window.setTimeout(() => {
      const line = notesRef.current?.querySelector<HTMLElement>("li.cited");
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      line?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    }, 0);
  };

  return (
    <>
      {meeting.data ? (
        <PageHeading
          title={meeting.data.body.title}
          lead={
            <>
              Meeting held {formatDate(meeting.data.body.held_at)} ·{" "}
              <StateBadge state={meeting.data.body.processing} />
            </>
          }
        />
      ) : (
        <>
          <PageHeading title="Meeting" />
          <QueryView query={meeting} loadingLabel="Loading meeting">
            {() => null}
          </QueryView>
        </>
      )}
      <p className="muted">
        Items below were proposed from the {isTranscript ? "recording’s transcript" : "notes"}. Nothing is adopted until you
        approve it.
      </p>
      <div className="split">
        <section aria-labelledby="items-title" className="split-main">
          <h2 id="items-title">Proposed items</h2>
          <QueryView
            query={reconcile}
            loadingLabel="Loading items"
            isEmpty={(d) => d.items.length === 0}
            empty={
              <>
                No items proposed yet. Ask your coding agent to extract proposals from these{" "}
                {isTranscript ? "transcript segments" : "notes"} (task purpose “meeting_extract”), or use the optional AI
                provider below.
              </>
            }
          >
            {(data) => (
              <ul className="card-list" data-testid="meeting-items">
                {data.items.map((item) => (
                  <MeetingItem
                    key={item.item_id}
                    item={item}
                    meetingTitle={meeting.data?.body.title ?? "meeting"}
                    selected={selected?.itemId === item.item_id}
                    onSelect={() => select(item)}
                    actionTitles={actions.records}
                    onPlay={isTranscript ? () => setSelected(evidenceSpan(item)) : undefined}
                  />
                ))}
              </ul>
            )}
          </QueryView>
          {reconcile.data ? <p className="muted small-print">{reconcile.data.note}</p> : null}
          <ProviderRunPanel meetingId={id} onDone={() => reconcile.reload()} />
        </section>
        <section
          aria-labelledby="notes-title"
          className="split-side"
          ref={notesRef}
          data-testid="original-pane"
        >
          <h2 id="notes-title">{isTranscript ? "Recording transcript (original)" : "Meeting notes (original)"}</h2>
          {versionId && isTranscript ? (
            <TranscriptPane versionId={versionId} selected={selected} />
          ) : versionId ? (
            version.data === undefined && !version.error ? (
              <Loading label="Loading notes" />
            ) : (
              <NotesText versionId={versionId} start={selected?.start} end={selected?.end} />
            )
          ) : meeting.data ? (
            <Empty>No retained notes are linked to this meeting.</Empty>
          ) : null}
        </section>
      </div>
    </>
  );
}

function TranscriptPane({ versionId, selected }: { versionId: string; selected: Selection | null }) {
  const view = useQuery<TranscriptView>(`/transcripts/${encodeURIComponent(versionId)}`);
  const [activeId, setActiveId] = useState<string | null>(null);
  const segments = view.data?.payload.segments ?? [];
  const { ref, playSpan } = useSpanPlayer((ms) => {
    const hit = segments.find((s) => s.start_ms <= ms && ms < s.end_ms);
    setActiveId(hit?.id ?? null);
  });
  if (view.data === undefined) {
    if (view.error) return <ErrorState error={view.error} onRetry={view.reload} />;
    return <Loading label="Loading transcript" />;
  }
  const t = view.data;
  const highlight =
    selected?.startMs !== undefined
      ? { start: selected.startMs, end: selected.endMs ?? selected.startMs }
      : null;
  return (
    <>
      {t.superseded_by_correction && t.latest_version_id ? (
        <p className="inline-warning" data-testid="transcript-corrected">
          A corrected version of this transcript exists. The items below still cite the version they were made from.{" "}
          <Link to={`/transcripts/${t.latest_version_id}`}>Open the corrected transcript</Link>
        </p>
      ) : null}
      {t.audio_version_id && t.audio_available ? (
        <AudioPlayer audioVersionId={t.audio_version_id} playerRef={ref} label="Meeting recording" />
      ) : (
        <p className="muted">The recording is not retained here; the transcript is.</p>
      )}
      {highlight ? (
        <p className="muted" aria-live="polite">
          Highlighting transcript {clock(highlight.start)}–{clock(highlight.end)}.
        </p>
      ) : (
        <p className="muted">Choose an item’s evidence to highlight the span it cites.</p>
      )}
      <TranscriptList
        segments={segments}
        label="Meeting transcript"
        highlight={highlight}
        activeId={activeId}
        onPlay={t.audio_version_id && t.audio_available ? (s) => playSpan(s.start_ms, s.end_ms) : undefined}
      />
      <p>
        <Link to={`/transcripts/${versionId}`}>Open the transcript page</Link> to correct a mistake.
      </p>
      <PlaySpanBridge selected={selected} audioReady={Boolean(t.audio_version_id && t.audio_available)} play={playSpan} />
    </>
  );
}

/** Lets an item's "Play this span" button reach the transcript pane's player. */
function PlaySpanBridge({
  selected,
  audioReady,
  play,
}: {
  selected: Selection | null;
  audioReady: boolean;
  play: (start: number, end: number) => void;
}) {
  if (!audioReady || selected?.startMs === undefined) return null;
  return (
    <p>
      <button
        type="button"
        className="button secondary small"
        data-testid="play-selected-span"
        onClick={() => play(selected.startMs ?? 0, selected.endMs ?? selected.startMs ?? 0)}
      >
        <span aria-hidden="true">▶ </span>Play the cited span ({clock(selected.startMs)}–{clock(selected.endMs ?? selected.startMs)})
      </button>
    </p>
  );
}

function NotesText({ versionId, start, end }: { versionId: string; start?: number; end?: number }) {
  const text = useQuery<VersionText>(`/versions/${encodeURIComponent(versionId)}/text`);
  if (text.data === undefined) {
    if (text.error) return <ErrorState error={text.error} onRetry={text.reload} />;
    return <Loading label="Loading notes" />;
  }
  if (text.data.text == null) {
    return (
      <div className="state state-unavailable" role="status">
        <p className="state-title">Notes unavailable</p>
        <p>Reason: {text.data.reason ?? "not stated"}.</p>
      </div>
    );
  }
  const lines = text.data.text.replace(/\n$/, "").split("\n");
  return (
    <>
      {start !== undefined ? (
        <p className="muted" aria-live="polite">
          Highlighting {lineLabel(start, end)}.
        </p>
      ) : (
        <p className="muted">Choose an item’s evidence to highlight the lines it cites.</p>
      )}
      <ol className="source-text" aria-label="Meeting notes text" data-testid="meeting-notes">
        {lines.map((line, index) => {
          const n = index + 1;
          const hit = start !== undefined && n >= start && n <= (end ?? start);
          return (
            <li key={n} value={n} className={hit ? "cited" : undefined} data-line={n}>
              {hit ? (
                <mark>
                  <span className="visually-hidden">Cited: </span>
                  {line || " "}
                </mark>
              ) : (
                line || " "
              )}
            </li>
          );
        })}
      </ol>
    </>
  );
}

function MeetingItem({
  item,
  meetingTitle,
  selected,
  onSelect,
  actionTitles,
  onPlay,
}: {
  item: ReconcileItem;
  meetingTitle: string;
  selected: boolean;
  onSelect: () => void;
  actionTitles: Map<string, Envelope>;
  onPlay?: () => void;
}) {
  const { announce, refresh, knownSeq, project } = useHub();
  const [created, setCreated] = useState<string | null>(null);
  const [dismissing, setDismissing] = useState<{ reason: string } | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const actionId = useMemo(() => `action_${uuid4()}`, []);
  const typeLabel = TYPE_LABELS[item.type] ?? item.type;
  const ev = item.evidence[0];
  const audio = ev?.locator.type === "audio";
  const span = item.audio_spans[0];
  const related = item.related_actions.filter((r) => !r.linked);
  const titleOf = (r: RelatedAction) => {
    const rec = actionTitles.get(r.action_id);
    return rec ? recordTitle(rec) : "an existing action";
  };

  const build = async () => {
    // Re-read the item and project so the preview reflects the current record.
    const [record, info] = await Promise.all([
      get<Envelope<ItemBody>>(`/records/${encodeURIComponent(item.item_id)}`),
      get<ProjectInfo>("/project"),
    ]);
    return {
      schema_version: 1,
      project_id: info.project_id,
      request_id: uuid4(),
      expected_seq: info.project_seq,
      operations: [
        {
          op: "create",
          kind: "action",
          id: actionId,
          base_rev: 0,
          sensitivity: record.sensitivity,
          occurred_at: null,
          body: {
            title: record.body.text.slice(0, 300),
            reason: `Adopted from the ${typeLabel.toLowerCase()} in “${meetingTitle}”: ${record.body.text}`,
            source_refs: [{ id: record.id, rev: record.rev }],
            owner_ref: null,
            due_at: null,
            dependencies: [],
            status: "proposed",
            blocker: null,
            completion_evidence: [],
            commitment_item_id: record.id,
          },
        },
        {
          op: "transition",
          id: actionId,
          base_rev: 1,
          to: "ready",
          reason: "Adopted by the researcher in the hub",
          evidence: [],
        },
        {
          op: "transition",
          id: record.id,
          base_rev: record.rev,
          to: "adopted",
          reason: "Adopted by the researcher in the hub",
          evidence: [],
        },
      ],
    };
  };

  const dismiss = async () => {
    if (!dismissing || !project) return;
    setBusy(true);
    setError(null);
    try {
      const record = await get<Envelope<ItemBody>>(`/records/${encodeURIComponent(item.item_id)}`);
      await post<CommitResult>("/transactions", {
        schema_version: 1,
        project_id: project.project_id,
        request_id: uuid4(),
        expected_seq: knownSeq,
        operations: [
          {
            op: "transition",
            id: item.item_id,
            base_rev: record.rev,
            to: "dismissed",
            reason: dismissing.reason.trim(),
            evidence: [],
          },
        ],
      });
      setDismissed(true);
      setDismissing(null);
      announce("Item dismissed. The reason is recorded.");
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li
      className={selected ? "card item-card selected" : "card item-card"}
      data-testid="meeting-item"
      data-item-type={item.type}
      data-item-id={item.item_id}
    >
      <div className="card-head">
        <p className="card-title">
          <span className="item-type">{typeLabel}:</span> {item.text}
        </p>
        <StateBadge state={item.status} prefix="Item state" />
      </div>
      <p className="card-meta">
        Owner: {item.owner === "unknown" ? "not agreed" : item.owner} · Due:{" "}
        {item.due === "unknown" ? "no agreed date" : item.due}
      </p>
      <p className="classification" data-testid="item-classification" data-classification={item.classification}>
        <strong>Compared with current work:</strong> {CLASS_TEXT[item.classification] ?? item.classification}
      </p>
      {related.length > 0 ? (
        <ul className="related-actions" data-testid="related-actions">
          {related.map((r) => (
            <li key={r.action_id} data-testid="related-action">
              <Link to={`/actions/${r.action_id}`}>{titleOf(r)}</Link>{" "}
              <span className="muted">
                ({r.status.replace(/_/g, " ")}, about {Math.round(r.overlap * 100)}% shared wording)
              </span>
              {item.status === "proposed" ? (
                <>
                  {" "}
                  <button
                    type="button"
                    className="button secondary small"
                    onClick={() =>
                      setDismissing({
                        reason: `Already covered by the action “${titleOf(r)}” (${r.action_id}).`,
                      })
                    }
                    aria-label={`Treat as already covered by ${titleOf(r)}`}
                  >
                    Already covered by this
                  </button>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {ev ? (
        <button
          type="button"
          className="source-button"
          onClick={onSelect}
          aria-pressed={selected}
          aria-label={`Show evidence in the ${audio ? "transcript" : "notes"}, ${spanLabel({
            startLine: ev.locator.start_line,
            endLine: ev.locator.end_line,
            startMs: audio ? (ev.locator.start_ms as number) : undefined,
            endMs: audio ? (ev.locator.end_ms as number) : undefined,
          })}`}
        >
          <span aria-hidden="true">¶ </span>Evidence: {audio ? "" : "notes "}
          {spanLabel({
            startLine: ev.locator.start_line,
            endLine: ev.locator.end_line,
            startMs: audio ? (ev.locator.start_ms as number) : undefined,
            endMs: audio ? (ev.locator.end_ms as number) : undefined,
          })}
        </button>
      ) : (
        <p className="muted">No evidence line cited.</p>
      )}
      {span ? (
        <blockquote className="audio-quote" data-testid="audio-quote">
          “{span.quote}”
          {span.uncertain ? <span className="badge badge-uncertain"> Uncertain or inaudible</span> : null}
          {span.text_changed_since ? (
            <p className="inline-warning" data-testid="audio-text-changed">
              The transcript was corrected after this item was proposed, and the cited words changed.{" "}
              {span.newer_version_id ? <Link to={`/transcripts/${span.newer_version_id}`}>See the corrected transcript</Link> : null}
            </p>
          ) : null}
        </blockquote>
      ) : null}
      {onPlay && audio ? (
        <button type="button" className="button secondary small" onClick={onPlay} data-testid="item-select-span">
          Select this span in the transcript
        </button>
      ) : null}
      {item.status === "proposed" && ADOPTABLE.has(item.type) && !created && !dismissed ? (
        <div className="item-actions">
          <DecisionReview
            startLabel={related.length > 0 ? "Adopt as a separate action anyway" : "Adopt as action"}
            title={`adopt this ${typeLabel.toLowerCase()}`}
            approveLabel="Approve and create action"
            affected={
              <ul>
                <li>
                  Creates one action: “{item.text}” (no owner, no date; you can assign them later).
                </li>
                <li>Marks this {typeLabel.toLowerCase()} as adopted and links it to the action.</li>
                {related.length > 0 ? (
                  <li>
                    It stays distinct from the similar action(s): {related.map((r) => titleOf(r)).join("; ")}.
                  </li>
                ) : null}
              </ul>
            }
            build={build}
            onApplied={(applied) => {
              const id = applied.result.results[0]?.id ?? actionId;
              setCreated(id);
              announce("Action created. It now appears on Now.");
            }}
          />
        </div>
      ) : null}
      {item.status === "proposed" && !dismissed && !created ? (
        dismissing ? (
          <div className="dismiss-form" data-testid="dismiss-form">
            <label htmlFor={`why-${item.item_id}`}>Why is this item being dismissed? (recorded with it)</label>
            <textarea
              id={`why-${item.item_id}`}
              rows={2}
              value={dismissing.reason}
              onChange={(e) => setDismissing({ reason: e.target.value })}
            />
            <div className="button-row">
              {/* dismiss() returns early without a project: the button waits for it rather than
                  accepting a click that does nothing. */}
              <button type="button" className="button primary small" disabled={busy || !project || !dismissing.reason.trim()} onClick={() => void dismiss()}>
                Dismiss this item
              </button>
              <button type="button" className="button secondary small" onClick={() => setDismissing(null)}>
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <p className="item-actions">
            <button
              type="button"
              className="button secondary small"
              onClick={() => setDismissing({ reason: "" })}
              data-testid="dismiss-start"
            >
              Dismiss…
            </button>
          </p>
        )
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      {item.type === "question" && item.status === "proposed" ? (
        <p className="muted">
          Open question with no agreed owner. It stays unowned until someone takes it on.
        </p>
      ) : null}
      {created ? (
        <p className="success" role="status" data-testid="adopted-notice">
          <span aria-hidden="true">✓ </span>Adopted. The new action is on <Link to="/now">Now</Link>.
        </p>
      ) : null}
      {dismissed ? (
        <p className="success" role="status" data-testid="dismissed-notice">
          <span aria-hidden="true">✓ </span>Dismissed, with the reason recorded.
        </p>
      ) : null}
      <Details>
        <KeyValues
          rows={[
            ["Item", <code key="i">{item.item_id}</code>],
            ["Classification", item.classification],
            ["Evidence version", <code key="v">{ev?.version_id ?? "none"}</code>],
          ]}
        />
      </Details>
    </li>
  );
}
