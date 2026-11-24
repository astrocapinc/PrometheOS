// Audio: visible recording with recoverable segments, import of existing recordings, local
// transcription with progress and cancel, and the transcript page (text alternative, playback,
// corrections). Capture runs on this computer in the hub's process; this page only controls
// and shows it. Nothing here records, uploads or plays by itself.
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { describeError, post, runOperation, uuid4 } from "../api";
import { AudioPlayer, TranscriptList, useSpanPlayer } from "../components/audio";
import { ModelActions } from "../components/parity";
import { Details, Empty, ErrorState, KeyValues, Loading, PageHeading, QueryView, StateBadge, clock } from "../components/ui";
import { useRecords } from "../data";
import { useHub, useQuery } from "../hub";
import { Link, useRouter } from "../router";
import type {
  AudioModels,
  CaptureDevice,
  Envelope,
  RecordingSession,
  RecordList,
  SourceBodyLite,
  TranscriptionRun,
  TranscriptView,
  VersionBody,
} from "../types";

export const RECORDING_EVENT = "prometheos-recording-changed";

function announceRecordingChange(): void {
  window.dispatchEvent(new Event(RECORDING_EVENT));
}

export const CONSENT_REMINDER =
  "Before recording, make sure every participant knows and has agreed, under the rules that apply to you.";

/** Re-run a query's reload on a timer while ``active`` (recordings and runs change outside the
 *  project sequence, so the change feed does not announce them). */
export function usePolling(reload: () => void, active: boolean, ms: number): void {
  const latest = useRef(reload);
  latest.current = reload;
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => latest.current(), ms);
    return () => window.clearInterval(id);
  }, [active, ms]);
}

function mmss(ms: number): string {
  return clock(ms);
}

// ----- the capture workspace --------------------------------------------------------------------

export function AudioCapture() {
  return (
    <div className="audio-workspace" data-testid="audio-workspace">
      <RecordPanel />
      <ImportPanel />
      <AttentionPanel />
      <RecentAudio />
    </div>
  );
}

function useSessions() {
  const sessions = useQuery<{ sessions: RecordingSession[] }>("/recordings");
  const active = (sessions.data?.sessions ?? []).some((s) => s.state === "recording" || s.state === "paused");
  usePolling(sessions.reload, active, 1000);
  return sessions;
}

function RecordPanel() {
  const { announce, refresh } = useHub();
  const devices = useQuery<{ backend: string; devices: CaptureDevice[] }>("/audio/devices");
  const sessions = useSessions();
  const [title, setTitle] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [sensitivity, setSensitivity] = useState("restricted");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const active = (sessions.data?.sessions ?? []).filter((s) => s.state === "recording" || s.state === "paused");

  const toggle = (id: string) =>
    setChosen((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id]));

  const start = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post<RecordingSession>("/recordings", {
        title: title.trim(),
        device_ids: chosen,
        sensitivity,
        segment_seconds: 30,
        consent_acknowledged: consent,
      });
      setTitle("");
      setConsent(false);
      announce("Recording started. The recorder shows its state on this page.");
      sessions.reload();
      announceRecordingChange();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const control = async (session: RecordingSession, action: string) => {
    try {
      await post<RecordingSession>(`/recordings/${encodeURIComponent(session.session_id)}/${action}`, {});
      announce(
        action === "stop"
          ? "Stopping and saving the recording."
          : action === "cancel"
            ? "Recording cancelled. Recorded segments are kept and can be recovered."
            : action === "pause"
              ? "Recording paused. Audio while paused is not kept."
              : "Recording resumed.",
      );
      sessions.reload();
      announceRecordingChange();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
    }
  };

  const ready = title.trim().length > 0 && chosen.length > 0 && consent && !busy;
  return (
    <section aria-labelledby="record-title" className="audio-section">
      <h2 id="record-title">Record a meeting</h2>
      {active.map((session) => (
        <ActiveRecording key={session.session_id} session={session} onControl={control} />
      ))}
      <QueryView query={devices} loadingLabel="Looking for capture devices">
        {(found) =>
          found.devices.length === 0 ? (
            <Empty>No microphones or system-audio sources were found on this computer.</Empty>
          ) : (
            <form className="capture-form" onSubmit={start} aria-labelledby="record-title" noValidate>
              <div className="field">
                <label htmlFor="rec-title">Recording title</label>
                <input id="rec-title" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
              </div>
              <fieldset className="type-choice">
                <legend>What to record (up to one microphone and one system-audio source)</legend>
                {found.devices.map((d) => (
                  <label key={d.id}>
                    <input
                      type="checkbox"
                      checked={chosen.includes(d.id)}
                      onChange={() => toggle(d.id)}
                      data-testid={`device-${d.id}`}
                    />{" "}
                    {d.name} <span className="muted">({d.kind === "microphone" ? "microphone" : "system audio"})</span>
                  </label>
                ))}
              </fieldset>
              <div className="field">
                <label htmlFor="rec-sens">Who can see this recording</label>
                <select id="rec-sens" value={sensitivity} onChange={(e) => setSensitivity(e.target.value)}>
                  <option value="restricted">Only me (restricted, hidden from coding agents)</option>
                  <option value="normal">My coding agents may read it (normal)</option>
                </select>
              </div>
              <div className="consent-box">
                <label>
                  <input
                    type="checkbox"
                    checked={consent}
                    onChange={(e) => setConsent(e.target.checked)}
                    data-testid="consent"
                  />{" "}
                  {CONSENT_REMINDER}
                </label>
              </div>
              {error ? <ErrorState error={error} /> : null}
              <button type="submit" className="button primary" disabled={!ready} aria-busy={busy}>
                {busy ? "Starting…" : "Start recording"}
              </button>
            </form>
          )
        }
      </QueryView>
      {devices.error && devices.data === undefined ? (
        <p className="muted small-print">
          Live recording needs the optional audio extra and Windows. Importing an existing recording
          below works without it.
        </p>
      ) : null}
    </section>
  );
}

function ActiveRecording({
  session,
  onControl,
}: {
  session: RecordingSession;
  onControl: (session: RecordingSession, action: string) => Promise<void>;
}) {
  const paused = session.state === "paused";
  return (
    <section
      className="card recording-card"
      aria-labelledby={`rec-${session.session_id}`}
      data-testid="recording-active"
      data-state={session.state}
    >
      <h3 id={`rec-${session.session_id}`}>
        <span aria-hidden="true">{paused ? "⏸ " : "● "}</span>
        {paused ? "Paused" : "Recording"}: {session.title}
      </h3>
      <p>
        Elapsed <strong data-testid="recording-elapsed">{mmss(session.elapsed_ms)}</strong> · input{" "}
        <span data-testid="recording-level">{session.level_dbfs} dBFS</span>{" "}
        <meter min={-80} max={0} value={Math.max(-80, session.level_dbfs)} aria-label="Input level">
          {session.level_dbfs} dBFS
        </meter>
      </p>
      <p className="muted">
        Recording: {session.sources.map((s) => s.name).join(" + ")}. Audio is saved in 30-second
        segments, so a crash or a lost device keeps what was recorded.
      </p>
      <div className="button-row">
        <button type="button" className="button secondary" onClick={() => void onControl(session, paused ? "resume" : "pause")}>
          {paused ? "Resume" : "Pause"}
        </button>
        <button type="button" className="button primary" onClick={() => void onControl(session, "stop")}>
          Stop and save
        </button>
        <button type="button" className="button secondary" onClick={() => void onControl(session, "cancel")}>
          Cancel (keep segments)
        </button>
      </div>
    </section>
  );
}

function ImportPanel() {
  const { project, announce, refresh } = useHub();
  const roots = useMemo(
    () => ((project?.settings as unknown as { allowed_roots?: { alias: string }[] })?.allowed_roots ?? []).map((r) => r.alias),
    [project],
  );
  const [root, setRoot] = useState("");
  const [path, setPath] = useState("");
  const [sensitivity, setSensitivity] = useState("restricted");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<{ version_id: string; duration: number | null } | null>(null);
  const chosenRoot = root || roots[0] || "";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const result = await runOperation<{ version_id: string; audio: { duration_ms: number } | null }>("/audio/import", {
        root_alias: chosenRoot,
        path: path.trim(),
        sensitivity,
      });
      setDone({ version_id: result.version_id, duration: result.audio?.duration_ms ?? null });
      setPath("");
      announce("Recording imported. It is retained exactly as it was.");
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="import-title" className="audio-section">
      <h2 id="import-title">Import an existing recording</h2>
      <form className="capture-form" onSubmit={submit} aria-labelledby="import-title" noValidate>
        <div className="field">
          <label htmlFor="imp-root">Research folder</label>
          <select id="imp-root" value={chosenRoot} onChange={(e) => setRoot(e.target.value)}>
            {roots.length === 0 ? <option value="">No research folder is approved</option> : null}
            {roots.map((alias) => (
              <option key={alias} value={alias}>
                {alias}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="imp-path">Audio file inside it</label>
          <input id="imp-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder="recordings/meeting.wav" />
          <p className="muted small-print">WAV needs nothing extra. mp3, m4a and others need the optional audio extra to transcribe.</p>
        </div>
        <div className="field">
          <label htmlFor="imp-sens">Who can see it</label>
          <select id="imp-sens" value={sensitivity} onChange={(e) => setSensitivity(e.target.value)}>
            <option value="restricted">Only me (restricted)</option>
            <option value="normal">My coding agents may read it (normal)</option>
          </select>
        </div>
        {error ? <ErrorState error={error} /> : null}
        {done ? (
          <p className="success" role="status" data-testid="import-done">
            <span aria-hidden="true">✓ </span>Imported
            {done.duration !== null ? ` (${mmss(done.duration)} long)` : ""}. Transcribe it below.
          </p>
        ) : null}
        <button type="submit" className="button primary" disabled={busy || !path.trim() || !chosenRoot} aria-busy={busy}>
          {busy ? "Importing…" : "Import recording"}
        </button>
      </form>
    </section>
  );
}

function AttentionPanel() {
  const { announce, refresh } = useHub();
  const sessions = useSessions();
  const [error, setError] = useState<unknown>(null);
  const [recovered, setRecovered] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const waiting = (sessions.data?.sessions ?? []).filter((s) => s.recoverable);
  if (waiting.length === 0 && !error) return null;

  const act = async (session: RecordingSession, action: "recover" | "discard") => {
    setError(null);
    try {
      await post(
        `/recordings/${encodeURIComponent(session.session_id)}/${action}${action === "discard" ? "?confirm=true" : ""}`,
        {},
      );
      setConfirming(null);
      if (action === "recover") {
        setRecovered(session.title);
        announce("Recovered the recorded part and saved it as a partial recording.");
      } else {
        announce("Discarded the unsaved segments.");
      }
      sessions.reload();
      void refresh().catch(() => undefined);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <section aria-labelledby="attention-title" className="audio-section" data-testid="recoverable-sessions">
      <h2 id="attention-title">Recordings that need attention</h2>
      <ul className="card-list">
        {waiting.map((session) => (
          <li key={session.session_id} className="card" data-testid="recoverable-session">
            <p className="card-title">{session.title}</p>
            <p className="card-meta">
              {session.state === "cancelled" ? "Cancelled" : "Interrupted"} after {mmss(session.elapsed_ms)}
              {session.reason ? ` · ${session.reason}` : session.abandoned ? " · the recorder stopped reporting" : ""}
            </p>
            <p className="muted">The recorded segments are still on this computer.</p>
            <div className="button-row">
              <button type="button" className="button primary" onClick={() => void act(session, "recover")}>
                Recover what was recorded
              </button>
              {confirming === session.session_id ? (
                <>
                  <button type="button" className="button primary" onClick={() => void act(session, "discard")}>
                    Yes, delete these segments for good
                  </button>
                  <button type="button" className="button secondary" onClick={() => setConfirming(null)}>
                    Keep them
                  </button>
                </>
              ) : (
                <button type="button" className="button secondary" onClick={() => setConfirming(session.session_id)}>
                  Discard the segments
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {recovered ? (
        <p className="success" role="status">
          <span aria-hidden="true">✓ </span>“{recovered}” was saved as a partial recording.
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </section>
  );
}

function RecentAudio() {
  const versions = useQuery<RecordList<VersionBody>>("/records?kind=version&limit=200");
  const runs = useQuery<{ runs: TranscriptionRun[] }>("/audio/transcriptions");
  const models = useQuery<AudioModels>("/audio/models");
  const running = (runs.data?.runs ?? []).some((r) => r.state === "queued" || r.state === "running");
  usePolling(runs.reload, running, 1000);
  const audio = (versions.data?.items ?? []).filter((v) => v.body.mime.startsWith("audio/"));
  const sourceIds = audio.map((v) => v.body.source_id);
  const sources = useRecords(sourceIds);
  return (
    <section aria-labelledby="recent-title" className="audio-section">
      <h2 id="recent-title">Recordings in this project</h2>
      <ModelNotice models={models.data} onChange={() => models.reload()} />
      <QueryView
        query={versions}
        loadingLabel="Loading recordings"
        isEmpty={() => audio.length === 0}
        empty={<>No recordings yet. Record or import one above.</>}
      >
        {() => (
          <ul className="card-list" data-testid="audio-list">
            {audio.map((version) => (
              <AudioRow
                key={version.id}
                version={version}
                title={(sources.records.get(version.body.source_id) as Envelope<SourceBodyLite> | undefined)?.body.title ?? "Recording"}
                runs={(runs.data?.runs ?? []).filter((r) => r.version_id === version.id)}
                models={models.data}
                onChange={() => runs.reload()}
              />
            ))}
          </ul>
        )}
      </QueryView>
    </section>
  );
}

function ModelNotice({ models, onChange }: { models: AudioModels | undefined; onChange: () => void }) {
  if (!models) return null;
  const real = models.status.models.find((m) => m.model === models.plan.model);
  if (real?.installed) {
    return (
      <>
        <p className="muted small-print" data-testid="model-installed">
          Local speech model installed: {models.plan.title}.
        </p>
        <Details label="Remove the local speech model">
          <ModelActions plan={models.plan} installed onDone={onChange} />
        </Details>
      </>
    );
  }
  return (
    <Details label="Local speech model is not installed">
      <p data-testid="model-plan">
        {models.plan.title}: {models.plan.total_mib} MiB, licence {models.plan.license}, revision{" "}
        <code>{models.plan.revision.slice(0, 12)}</code>. It would be stored on this computer only.
        Nothing is downloaded until you review the plan and run:
      </p>
      <pre className="command">prometheos audio model plan{"\n"}prometheos audio model install --plan-sha256 {models.plan.plan_sha256}</pre>
    </Details>
  );
}

function AudioRow({
  version,
  title,
  runs,
  models,
  onChange,
}: {
  version: Envelope<VersionBody>;
  title: string;
  runs: TranscriptionRun[];
  models: AudioModels | undefined;
  onChange: () => void;
}) {
  const { announce } = useHub();
  const latest = runs[0];
  const active = latest && (latest.state === "queued" || latest.state === "running");
  const installed = models?.status.models.some((m) => m.installed) ?? false;
  const [engine, setEngine] = useState("");
  const chosen = engine || (installed ? "faster-whisper" : models?.synthetic_enabled ? "synthetic" : "faster-whisper");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      await post<TranscriptionRun>("/audio/transcriptions", { version_id: version.id, engine: chosen });
      announce("Transcription started on this computer.");
      onChange();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    if (!latest) return;
    try {
      await post(`/audio/transcriptions/${encodeURIComponent(latest.run_id)}/cancel`, {});
      announce("Cancelling the transcription before its next chunk.");
      onChange();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <li className="card" data-testid="audio-row" data-version={version.id}>
      <div className="card-head">
        <p className="card-title">{title}</p>
        <StateBadge state={version.sensitivity === "normal" ? "available" : "review"} prefix="Visibility" />
      </div>
      <p className="card-meta">
        {version.sensitivity === "normal" ? "Visible to coding agents" : "Only you can see it"} ·{" "}
        {Math.round(version.body.byte_length / 1024)} KiB · {version.body.availability}
      </p>
      {latest ? (
        <p data-testid="run-state" data-state={latest.state} aria-live="off">
          Transcription: <strong>{runLabel(latest)}</strong>
          {latest.error ? <span className="muted"> · {latest.error}</span> : null}{" "}
          {latest.state === "completed" && latest.result_version_id ? (
            <Link to={`/transcripts/${latest.result_version_id}`}>Open the transcript</Link>
          ) : null}
        </p>
      ) : (
        <p className="muted">Not transcribed yet.</p>
      )}
      <div className="button-row">
        <div className="field inline-field">
          <label htmlFor={`engine-${version.id}`}>Engine</label>
          <select id={`engine-${version.id}`} value={chosen} onChange={(e) => setEngine(e.target.value)}>
            <option value="faster-whisper">Local speech model (faster-whisper)</option>
            {models?.synthetic_enabled ? <option value="synthetic">Synthetic test engine (not a real transcript)</option> : null}
          </select>
        </div>
        <button type="button" className="button primary" onClick={() => void start()} disabled={busy || Boolean(active)}>
          {latest && ["failed"].includes(latest.state) ? "Resume transcription" : "Transcribe on this computer"}
        </button>
        {active ? (
          <button type="button" className="button secondary" onClick={() => void cancel()}>
            Cancel transcription
          </button>
        ) : null}
      </div>
      {error ? <ErrorState error={error} /> : null}
      <p className="muted small-print">
        Transcription runs here; nothing leaves this computer. Recordings and their transcripts are only sent anywhere through the AI provider settings, with a preview.
      </p>
    </li>
  );
}

function runLabel(run: TranscriptionRun): string {
  switch (run.state) {
    case "queued":
      return "waiting to start";
    case "running":
      return `running, chunk ${Math.min(run.chunks_done + 1, run.chunk_count ?? run.chunks_done + 1)} of ${run.chunk_count ?? "?"}${run.cancel_requested ? " (cancelling)" : ""}`;
    case "completed":
      return "finished";
    case "cancelled":
      return "cancelled";
    case "failed":
      return "stopped with an error (can be resumed)";
    default:
      return run.state;
  }
}

// ----- the transcript page ----------------------------------------------------------------------

export function TranscriptPage({ id }: { id: string }) {
  const { navigate } = useRouter();
  const { announce, refresh } = useHub();
  const view = useQuery<TranscriptView>(`/transcripts/${encodeURIComponent(id)}`);
  const [activeId, setActiveId] = useState<string | null>(null);
  const segments = view.data?.payload.segments ?? [];
  const { ref, playSpan } = useSpanPlayer((ms) => {
    const hit = segments.find((s) => s.start_ms <= ms && ms < s.end_ms);
    setActiveId(hit?.id ?? null);
  });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [title, setTitle] = useState("Meeting recording");
  const [heldAt, setHeldAt] = useState("");

  const data = view.data;
  const latest = data ? !data.superseded_by_correction : true;

  const save = async (segmentId: string) => {
    setBusy(true);
    setError(null);
    try {
      const reply = await post<{ version_id: string }>(`/transcripts/${encodeURIComponent(id)}/corrections`, {
        edits: [{ segment_id: segmentId, text: draft }],
        request_id: uuid4(),
      });
      setEditing(null);
      announce("Correction saved as a new version of the transcript. The earlier version is kept.");
      void refresh().catch(() => undefined);
      navigate(`/transcripts/${reply.version_id}`);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  const makeMeeting = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const reply = await post<{ meeting_id: string }>("/meetings/from-audio", {
        transcript_version_id: id,
        audio_version_id: data?.audio_version_id ?? null,
        title: title.trim(),
        held_at: heldAt ? new Date(heldAt).toISOString().replace(/\.\d{3}Z$/, "Z") : null,
        request_id: uuid4(),
      });
      announce("Meeting created from the transcript.");
      void refresh().catch(() => undefined);
      navigate(`/meetings/${reply.meeting_id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeading
        title="Transcript"
        lead={data ? <>Text version of a recording, with times. Segments marked uncertain were hard to hear.</> : undefined}
      />
      <QueryView query={view} loadingLabel="Loading transcript">
        {(t) => (
          <>
            {t.superseded_by_correction && t.latest_version_id ? (
              <p className="inline-warning" role="status" data-testid="superseded-banner">
                A corrected version of this transcript exists.{" "}
                <Link to={`/transcripts/${t.latest_version_id}`}>Open the latest version</Link>
              </p>
            ) : null}
            {t.audio_version_id && t.audio_available ? (
              <AudioPlayer audioVersionId={t.audio_version_id} playerRef={ref} label="Recording" />
            ) : (
              <p className="muted">The recording itself is not available here; the transcript text is.</p>
            )}
            {segments.length === 0 ? (
              <Empty>No speech was found in this recording.</Empty>
            ) : (
              <TranscriptList
                segments={segments}
                label="Transcript segments"
                activeId={activeId}
                onPlay={t.audio_version_id && t.audio_available ? (s) => playSpan(s.start_ms, s.end_ms) : undefined}
                scrollToHighlight={false}
                renderExtra={
                  latest
                    ? (s) =>
                        editing === s.id ? (
                          <div className="correct-form">
                            <label htmlFor={`fix-${s.id}`}>Corrected text for {clock(s.start_ms)}</label>
                            <textarea id={`fix-${s.id}`} rows={3} value={draft} onChange={(e) => setDraft(e.target.value)} />
                            <div className="button-row">
                              <button type="button" className="button primary small" disabled={busy || !draft.trim()} onClick={() => void save(s.id)}>
                                Save correction
                              </button>
                              <button type="button" className="button secondary small" onClick={() => setEditing(null)}>
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="button secondary small seg-fix"
                            onClick={() => {
                              setEditing(s.id);
                              setDraft(s.text);
                            }}
                            aria-label={`Correct the text at ${clock(s.start_ms)}`}
                          >
                            Correct
                          </button>
                        )
                    : undefined
                }
              />
            )}
            {error ? <ErrorState error={error} /> : null}
            {latest ? (
              <form className="capture-form" onSubmit={makeMeeting} aria-labelledby="mk-title" noValidate>
                <h2 id="mk-title">Use this as a meeting</h2>
                <p className="muted">
                  Creates a meeting whose original material is the recording and this transcript. Your coding agent can then propose items from it; you review each one.
                </p>
                <div className="field">
                  <label htmlFor="mk-name">Meeting title</label>
                  <input id="mk-name" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="mk-held">
                    Held at <span className="muted">(optional)</span>
                  </label>
                  <input id="mk-held" type="datetime-local" value={heldAt} onChange={(e) => setHeldAt(e.target.value)} />
                </div>
                <button type="submit" className="button primary" disabled={busy || !title.trim()}>
                  Create meeting
                </button>
              </form>
            ) : null}
            <Details>
              <KeyValues
                rows={[
                  ["Version", <code key="v">{t.version_id}</code>],
                  ["Made by", t.transformation ? `${t.transformation.engine} ${t.transformation.engine_version} (${t.transformation.type})` : "unknown"],
                  ["Model", t.transformation?.model ?? "none"],
                  ["Language", t.transformation?.language ?? "not stated"],
                  ["Length", clock(t.duration_ms)],
                  ["Visibility", t.sensitivity],
                ]}
              />
            </Details>
          </>
        )}
      </QueryView>
      {view.data === undefined && !view.error && !view.loading ? <Loading label="Loading" /> : null}
    </>
  );
}

/** A banner shown in the header on every page while a recording is running. */
export function RecordingBanner() {
  const sessions = useQuery<{ sessions: RecordingSession[] }>("/recordings");
  const active = (sessions.data?.sessions ?? []).filter((s) => s.state === "recording" || s.state === "paused");
  usePolling(sessions.reload, true, active.length > 0 ? 2000 : 8000);
  const { reload } = sessions;
  useEffect(() => {
    window.addEventListener(RECORDING_EVENT, reload);
    return () => window.removeEventListener(RECORDING_EVENT, reload);
  }, [reload]);
  if (active.length === 0) return null;
  return (
    <p className="recording-banner" data-testid="recording-banner">
      {active.map((s) => (
        <span key={s.session_id}>
          <span aria-hidden="true">{s.state === "paused" ? "⏸ " : "● "}</span>
          <strong>{s.state === "paused" ? "Recording paused" : "Recording in progress"}</strong>: {s.title} · {mmss(s.elapsed_ms)}{" "}
        </span>
      ))}
      <Link to="/capture?type=audio">Open the recorder</Link>
    </p>
  );
}

