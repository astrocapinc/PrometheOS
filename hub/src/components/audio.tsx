// Transcript building blocks shared by the transcript page, the meeting review and the
// source panel. A transcript is the text alternative of a recording: every segment shows its
// time span, uncertain spans are labelled in words, and playing a span never starts by itself.
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import type { TranscriptSegment } from "../types";
import { clock } from "./ui";

export interface SpanRange {
  start: number;
  end: number;
}

export function overlaps(segment: TranscriptSegment, range: SpanRange | null | undefined): boolean {
  if (!range) return false;
  if (range.end === range.start) return segment.start_ms <= range.start && range.start < segment.end_ms;
  return segment.start_ms < range.end && segment.end_ms > range.start;
}

export function TranscriptList({
  segments,
  label,
  highlight,
  activeId,
  onPlay,
  renderExtra,
  scrollToHighlight = true,
}: {
  segments: TranscriptSegment[];
  label: string;
  highlight?: SpanRange | null;
  activeId?: string | null;
  onPlay?: (segment: TranscriptSegment) => void;
  renderExtra?: (segment: TranscriptSegment) => ReactNode;
  scrollToHighlight?: boolean;
}) {
  const firstHit = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (!highlight || !scrollToHighlight) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    firstHit.current?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [highlight?.start, highlight?.end, scrollToHighlight]); // eslint-disable-line react-hooks/exhaustive-deps
  let marked = false;
  return (
    <ol className="source-text transcript-list" aria-label={label} data-testid="transcript-list">
      {segments.map((segment) => {
        const hit = overlaps(segment, highlight);
        const first = hit && !marked;
        if (first) marked = true;
        const active = activeId === segment.id;
        return (
          <li
            key={segment.id}
            ref={first ? (firstHit as RefObject<HTMLLIElement>) : undefined}
            className={[hit ? "cited" : "", active ? "playing" : ""].filter(Boolean).join(" ") || undefined}
            aria-current={active ? "true" : undefined}
            data-testid="transcript-segment"
            data-segment={segment.id}
            data-start-ms={segment.start_ms}
          >
            <span className="seg-time">
              <span className="visually-hidden">From </span>
              {clock(segment.start_ms)}
              <span aria-hidden="true">–</span>
              <span className="visually-hidden"> to </span>
              {clock(segment.end_ms)}
            </span>{" "}
            <span className="seg-text">
              {hit ? (
                <mark>
                  <span className="visually-hidden">Cited: </span>
                  {segment.text}
                </mark>
              ) : (
                segment.text
              )}
            </span>
            {segment.uncertain ? (
              <span className="badge badge-uncertain" data-testid="uncertain-badge">
                Uncertain or inaudible
              </span>
            ) : null}
            {segment.speaker_label ? <span className="muted"> · speaker label: {segment.speaker_label}</span> : null}
            {onPlay ? (
              <button
                type="button"
                className="button secondary small seg-play"
                onClick={() => onPlay(segment)}
                aria-label={`Play ${clock(segment.start_ms)} to ${clock(segment.end_ms)}`}
              >
                <span aria-hidden="true">▶ </span>Play
              </button>
            ) : null}
            {renderExtra ? renderExtra(segment) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** The browser's own audio element for a retained recording. Never autoplays. The caller
 *  drives it through the returned handle (seek and play a span, stop at its end). */
export interface PlayerHandle {
  element: HTMLAudioElement | null;
  playSpan: (startMs: number, endMs: number) => void;
}

export function useSpanPlayer(onTick?: (ms: number) => void) {
  const ref = useRef<HTMLAudioElement>(null);
  const stopAt = useRef<number | null>(null);
  const tick = useRef(onTick);
  tick.current = onTick;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = () => {
      const ms = el.currentTime * 1000;
      tick.current?.(ms);
      if (stopAt.current !== null && ms >= stopAt.current) {
        el.pause();
        stopAt.current = null;
      }
    };
    el.addEventListener("timeupdate", handler);
    return () => el.removeEventListener("timeupdate", handler);
  });
  const playSpan = (startMs: number, endMs: number) => {
    const el = ref.current;
    if (!el) return;
    stopAt.current = endMs;
    el.currentTime = startMs / 1000;
    void el.play().catch(() => undefined);
  };
  return { ref, playSpan };
}

export function AudioPlayer({
  audioVersionId,
  playerRef,
  label,
}: {
  audioVersionId: string;
  playerRef: RefObject<HTMLAudioElement | null>;
  label: string;
}) {
  return (
    <figure className="audio-player">
      <audio
        ref={playerRef as RefObject<HTMLAudioElement>}
        controls
        preload="metadata"
        src={`/api/v1/versions/${encodeURIComponent(audioVersionId)}/content`}
        aria-label={label}
        data-testid="audio-element"
      />
      <figcaption className="muted small-print">
        The recording plays only when you press play. The transcript beside it is the text
        version of the same audio.
      </figcaption>
    </figure>
  );
}
