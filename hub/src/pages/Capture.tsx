// Capture: a small form; type-specific fields appear after choosing a type.
// Typed notes are ordinary writes (POST /transactions) against the project sequence the
// user is looking at; a 409 keeps the draft and offers refresh-and-review, never a silent
// overwrite. Meeting notes go to POST /meetings with a stable request ID (retry-safe).
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, describeError, post, uuid4 } from "../api";
import { PageHeading } from "../components/ui";
import { useHandoff } from "../handoff";
import { useHub } from "../hub";
import { Link, useRouter } from "../router";
import type { CommitResult } from "../types";
import { AudioCapture } from "./Audio";

type CaptureType = "note" | "meeting" | "audio";

const CATEGORIES = [
  ["note", "Note"],
  ["finding", "Finding"],
  ["question", "Question"],
  ["decision", "Decision"],
  ["experiment", "Experiment"],
] as const;

export function CapturePage() {
  const { search } = useRouter();
  const wanted = new URLSearchParams(search).get("type");
  const initial: CaptureType = wanted === "meeting" ? "meeting" : wanted === "audio" ? "audio" : "note";
  const [type, setType] = useState<CaptureType>(initial);
  return (
    <>
      <PageHeading title="Capture" lead="Add a typed note or meeting notes to the project record." />
      <fieldset className="type-choice">
        <legend>What are you capturing?</legend>
        <label>
          <input
            type="radio"
            name="capture-type"
            value="note"
            checked={type === "note"}
            onChange={() => setType("note")}
          />{" "}
          Typed note
        </label>
        <label>
          <input
            type="radio"
            name="capture-type"
            value="meeting"
            checked={type === "meeting"}
            onChange={() => setType("meeting")}
          />{" "}
          Meeting notes
        </label>
        <label>
          <input
            type="radio"
            name="capture-type"
            value="audio"
            checked={type === "audio"}
            onChange={() => setType("audio")}
          />{" "}
          Recording or audio file
        </label>
      </fieldset>
      {type === "audio" ? null : (
        <p className="muted small-print">
          Other files are imported through your coding agent or the CLI in this version.
        </p>
      )}
      {type === "note" ? <NoteForm /> : type === "meeting" ? <MeetingForm /> : <AudioCapture />}
    </>
  );
}

type Outcome =
  | { kind: "none" }
  | { kind: "saved"; id: string; title: string }
  | { kind: "stale"; message: string }
  | { kind: "refreshed" }
  | { kind: "error"; error: unknown };

function NoteForm() {
  const { project, knownSeq, refresh, announce } = useHub();
  const handed = useHandoff("capture", (value) => setText(value));
  const [title, setTitle] = useState("");
  const [text, setText] = useState(handed);
  const [category, setCategory] = useState("note");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "none" });
  const [fieldError, setFieldError] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const outcomeRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (outcome.kind !== "none") outcomeRef.current?.focus();
  }, [outcome]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim() || !text.trim()) {
      setFieldError("Give the note a title and some text.");
      titleRef.current?.focus();
      return;
    }
    if (!project) return;
    setFieldError(null);
    setBusy(true);
    const id = `entry_${uuid4()}`;
    const request = {
      schema_version: 1,
      project_id: project.project_id,
      request_id: uuid4(),
      expected_seq: knownSeq,
      operations: [
        {
          op: "create",
          kind: "entry",
          id,
          base_rev: 0,
          sensitivity: "normal",
          occurred_at: null,
          body: {
            title: title.trim(),
            text: text.trim(),
            category,
            evidence: [],
            claim_refs: [],
            author_ref: null,
            origin: "typed",
            quantities: [],
            review: "reviewed",
          },
        },
      ],
    };
    try {
      await post<CommitResult>("/transactions", request);
      const saved = title.trim();
      setTitle("");
      setText("");
      setCategory("note");
      setOutcome({ kind: "saved", id, title: saved });
      announce(`Saved note “${saved}” to the notebook.`);
      void refresh().catch(() => undefined);
    } catch (error) {
      if (error instanceof ApiError && error.kind === "stale") {
        setOutcome({ kind: "stale", message: error.message });
        announce("The project changed since you loaded it. Your draft is kept.");
      } else {
        setOutcome({ kind: "error", error });
        announce(describeError(error).title);
      }
    } finally {
      setBusy(false);
    }
  };

  const refreshAndReview = async () => {
    try {
      await refresh();
      setOutcome({ kind: "refreshed" });
      announce("Refreshed. Review your draft, then save again.");
    } catch (error) {
      setOutcome({ kind: "error", error });
    }
  };

  return (
    <form className="capture-form" onSubmit={submit} aria-labelledby="note-form-title" noValidate>
      <h2 id="note-form-title">Typed note</h2>
      <div className="field">
        <label htmlFor="note-title">Title</label>
        <input
          id="note-title"
          ref={titleRef}
          value={title}
          maxLength={300}
          onChange={(e) => setTitle(e.target.value)}
          aria-invalid={fieldError && !title.trim() ? true : undefined}
          required
        />
      </div>
      <div className="field">
        <label htmlFor="note-text">Note</label>
        <textarea
          id="note-text"
          value={text}
          rows={6}
          onChange={(e) => setText(e.target.value)}
          aria-invalid={fieldError && !text.trim() ? true : undefined}
          required
        />
      </div>
      <div className="field">
        <label htmlFor="note-category">Kind of note</label>
        <select id="note-category" value={category} onChange={(e) => setCategory(e.target.value)}>
          {CATEGORIES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
      {fieldError ? (
        <p className="field-error" role="alert">
          {fieldError}
        </p>
      ) : null}
      <div ref={outcomeRef} tabIndex={-1} className="outcome">
        {outcome.kind === "saved" ? (
          <p className="success" data-testid="capture-saved">
            <span aria-hidden="true">✓ </span>Saved “{outcome.title}” to the notebook.{" "}
            <Link to={`/notebook#${outcome.id}`}>View it in the Notebook</Link>
          </p>
        ) : null}
        {outcome.kind === "stale" ? (
          <div className="stale-notice" role="alert" data-testid="stale-notice">
            <p className="state-title">The project changed since you loaded it</p>
            <p>
              Nothing was saved and your draft is kept below. Refresh to load the latest changes,
              review your note, then save again.
            </p>
            <button type="button" className="button secondary" onClick={() => void refreshAndReview()}>
              Refresh and review
            </button>
          </div>
        ) : null}
        {outcome.kind === "refreshed" ? (
          <p className="info" data-testid="refreshed-notice">
            Latest changes loaded. Your draft is unchanged; save when ready.
          </p>
        ) : null}
        {outcome.kind === "error" ? (
          <div className="state state-error" role="alert">
            <p className="state-title">{describeError(outcome.error).title}</p>
            <p>{describeError(outcome.error).detail} Your draft is kept.</p>
          </div>
        ) : null}
      </div>
      {/* submit() returns early without a project: until it has loaded the button stays disabled
          rather than accepting a click that does nothing. */}
      <button type="submit" className="button primary" disabled={busy || !project} aria-busy={busy}>
        {busy ? "Saving…" : "Save note"}
      </button>
    </form>
  );
}

function MeetingForm() {
  const { navigate } = useRouter();
  const { announce, refresh } = useHub();
  const [title, setTitle] = useState("");
  const [heldAt, setHeldAt] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  // One request ID per draft: retrying after a network failure cannot create duplicates.
  const requestId = useRef(uuid4());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim() || !text.trim()) {
      setFieldError("Give the meeting a title and paste or type the notes.");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const held = heldAt ? new Date(heldAt).toISOString().replace(/\.\d{3}Z$/, "Z") : undefined;
      const reply = await post<{ meeting_id: string }>("/meetings", {
        title: title.trim(),
        text,
        held_at: held,
        request_id: requestId.current,
      });
      announce(`Meeting notes “${title.trim()}” captured.`);
      void refresh().catch(() => undefined);
      navigate(`/meetings/${reply.meeting_id}`);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="capture-form" onSubmit={submit} aria-labelledby="meeting-form-title" noValidate>
      <h2 id="meeting-form-title">Meeting notes</h2>
      <div className="field">
        <label htmlFor="meeting-title">Meeting title</label>
        <input
          id="meeting-title"
          value={title}
          maxLength={300}
          onChange={(e) => setTitle(e.target.value)}
          required
        />
      </div>
      <div className="field">
        <label htmlFor="meeting-held">
          Held at <span className="muted">(optional)</span>
        </label>
        <input
          id="meeting-held"
          type="datetime-local"
          value={heldAt}
          onChange={(e) => setHeldAt(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="meeting-text">Notes</label>
        <textarea
          id="meeting-text"
          value={text}
          rows={10}
          onChange={(e) => setText(e.target.value)}
          required
        />
        <p className="muted small-print">
          Notes are retained exactly as typed. Your coding agent can propose items from them; you
          review each one here.
        </p>
      </div>
      {fieldError ? (
        <p className="field-error" role="alert">
          {fieldError}
        </p>
      ) : null}
      {error ? (
        <div className="state state-error" role="alert">
          <p className="state-title">{describeError(error).title}</p>
          <p>{describeError(error).detail} Your notes are kept.</p>
        </div>
      ) : null}
      <button type="submit" className="button primary" disabled={busy} aria-busy={busy}>
        {busy ? "Capturing…" : "Capture meeting notes"}
      </button>
    </form>
  );
}
