// The side context panel: the original retained text of a source version, with the cited
// line span highlighted. Non-modal (complementary landmark); focus moves to it on open and
// returns to the invoking control on close. Unavailable sources say so with the reason.
import { useEffect, useRef } from "react";
import { useHub, useQuery } from "../hub";
import type { TranscriptView, VersionText } from "../types";
import { TranscriptList } from "./audio";
import { Details, ErrorState, KeyValues, Loading, lineLabel, spanLabel } from "./ui";

export function ContextPanel() {
  const { source, closeSource } = useHub();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const highlightRef = useRef<HTMLLIElement>(null);
  const audio = source?.startMs !== undefined;
  const query = useQuery<VersionText>(
    source && !audio ? `/versions/${encodeURIComponent(source.versionId)}/text` : null,
  );
  const transcript = useQuery<TranscriptView>(
    source && audio ? `/transcripts/${encodeURIComponent(source.versionId)}` : null,
  );

  useEffect(() => {
    if (source) headingRef.current?.focus();
  }, [source]);

  useEffect(() => {
    if (!source || !query.data?.text) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    highlightRef.current?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [source, query.data]);

  useEffect(() => {
    if (!source) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // An open dialog (the command palette, the shortcut sheet) takes Escape first.
        if (event.defaultPrevented || document.querySelector("dialog[open]")) return;
        event.preventDefault();
        closeSource();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [source, closeSource]);

  if (!source) return null;
  const start = source.startLine;
  const end = source.endLine ?? source.startLine;
  const data = query.data;
  const lines = data?.text != null ? data.text.replace(/\n$/, "").split("\n") : [];
  let firstHighlight = true;

  return (
    <aside className="context-panel" aria-labelledby="context-title" data-testid="context-panel">
      <div className="context-head">
        <h2 id="context-title" tabIndex={-1} ref={headingRef}>
          Source: {source.title}
        </h2>
        <button type="button" className="button secondary small" onClick={closeSource}>
          Close<span className="visually-hidden"> source panel</span>
        </button>
      </div>
      <p className="context-meta">
        {audio ? "Transcript of the recording, " : "Original retained text, "}
        {audio ? spanLabel(source) : lineLabel(start, end)} highlighted.
        {source.citedBy ? <> Cited by: {source.citedBy}</> : null}
      </p>
      {audio && transcript.data === undefined && transcript.loading ? (
        <Loading label="Loading transcript" />
      ) : null}
      {audio && transcript.data === undefined && transcript.error ? (
        <ErrorState error={transcript.error} onRetry={transcript.reload} />
      ) : null}
      {audio && transcript.data ? (
        <>
          {transcript.data.superseded_by_correction ? (
            <p className="inline-warning">
              This is the version that was cited. A later corrected version of this transcript
              exists.
            </p>
          ) : null}
          <TranscriptList
            segments={transcript.data.payload.segments}
            label={`Transcript of ${source.title}`}
            highlight={{ start: source.startMs ?? 0, end: source.endMs ?? source.startMs ?? 0 }}
          />
        </>
      ) : null}
      {!audio && data === undefined && query.loading ? (
        <Loading label="Loading source text" />
      ) : null}
      {!audio && data === undefined && query.error ? (
        <ErrorState error={query.error} onRetry={query.reload} />
      ) : null}
      {data && data.text == null ? (
        <div className="state state-unavailable" role="status" data-testid="source-unavailable">
          <p className="state-title">Source text unavailable</p>
          <p>Reason: {data.reason ?? "not stated"}. No excerpt is shown because none is retained.</p>
        </div>
      ) : null}
      {data && data.text != null ? (
        <>
          {data.truncated ? (
            <p className="inline-warning">Only the first part of this source is shown.</p>
          ) : null}
          <ol className="source-text" aria-label={`Text of ${source.title}`}>
            {lines.map((line, index) => {
              const n = index + 1;
              const hit = start !== undefined && n >= start && n <= (end ?? start);
              const isFirst = hit && firstHighlight;
              if (isFirst) firstHighlight = false;
              return (
                <li
                  key={n}
                  value={n}
                  className={hit ? "cited" : undefined}
                  ref={isFirst ? highlightRef : undefined}
                  data-line={n}
                >
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
          <Details>
            <KeyValues
              rows={[
                ["Version", <code key="v">{data.version_id}</code>],
                ["Source", <code key="s">{data.source_id}</code>],
                ["Availability", data.availability],
                ["Type", data.mime],
                ["Size", `${data.byte_length} bytes`],
              ]}
            />
          </Details>
        </>
      ) : null}
    </aside>
  );
}
