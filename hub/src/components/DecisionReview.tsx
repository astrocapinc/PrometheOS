// Two-step owner decision: preview (shows the protected effects the server computed for
// exactly this request) and only then an explicit Approve that applies that request with
// the previewed digest. Apply is never sent without a preview on screen.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError, describeError, post } from "../api";
import { useHub } from "../hub";
import type { DecisionApplied, DecisionPreview } from "../types";
import { Details } from "./ui";

export type DecisionRequest = Record<string, unknown>;

type Phase =
  | { name: "idle" }
  | { name: "previewing" }
  | { name: "preview"; request: DecisionRequest; preview: DecisionPreview }
  | { name: "applying"; request: DecisionRequest; preview: DecisionPreview }
  | { name: "error"; error: unknown; request?: DecisionRequest; preview?: DecisionPreview };

const EFFECT_TEXT: Record<string, string> = {
  "action proposed->ready": "A new action is created and marked ready (adopted work).",
  "item proposed->adopted": "The meeting item is marked adopted and linked to that action.",
  "apply correction": "The correction is applied: its effects change the affected records.",
  "completion without evidence": "The work item is marked done on your note, with no linked evidence.",
  "dependency waiver": "The work item is marked ready although work it depends on is not done.",
};

export function describeEffect(effect: string): string {
  return EFFECT_TEXT[effect] ?? effect;
}

export function RootChanges({ changes }: { changes?: { alias: string; old_path: string | null; new_path: string | null }[] }) {
  if (!changes?.length) return null;
  return <ul className="effects" data-testid="root-changes">{changes.map((root) => <li key={root.alias}><strong>{root.alias}</strong>: <code>{root.old_path ?? "(absent)"}</code> → <code>{root.new_path ?? "(removed)"}</code></li>)}</ul>;
}

export function DecisionReview({
  startLabel,
  title,
  affected,
  approveLabel = "Approve",
  build,
  onApplied,
  disabled,
}: {
  startLabel: string;
  title: string;
  affected: ReactNode;
  approveLabel?: string;
  build: () => Promise<DecisionRequest>;
  onApplied: (applied: DecisionApplied) => void;
  disabled?: boolean;
}) {
  const { announce, refresh } = useHub();
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const regionRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    if (phase.name === "preview" || phase.name === "error") regionRef.current?.focus();
  }, [phase.name]);

  const preview = async () => {
    setPhase({ name: "previewing" });
    try {
      const request = await build();
      const result = await post<DecisionPreview>("/decisions/preview", { request });
      setPhase({ name: "preview", request, preview: result });
      announce(`Review: ${result.protected_effects.length} protected effect(s). Approve to apply.`);
    } catch (error) {
      setPhase({ name: "error", error });
      announce(describeError(error).title);
    }
  };

  const approve = async () => {
    if (phase.name !== "preview") return;
    const { request, preview: shown } = phase;
    setPhase({ name: "applying", request, preview: shown });
    try {
      const applied = await post<DecisionApplied>("/decisions/apply", {
        request,
        digest: shown.digest,
      });
      setPhase({ name: "idle" });
      onApplied(applied);
      void refresh().catch(() => undefined);
    } catch (error) {
      setPhase({ name: "error", error, request, preview: shown });
      announce(describeError(error).title);
    }
  };

  if (phase.name === "idle" || phase.name === "previewing") {
    return (
      <button
        type="button"
        className="button primary"
        onClick={preview}
        disabled={disabled || phase.name === "previewing"}
        aria-busy={phase.name === "previewing"}
      >
        {phase.name === "previewing" ? "Preparing preview…" : startLabel}
      </button>
    );
  }

  if (phase.name === "error") {
    const stale = phase.error instanceof ApiError && phase.error.kind === "stale";
    const { title: errTitle, detail } = describeError(phase.error);
    return (
      <div className="decision decision-error" tabIndex={-1} ref={regionRef} role="alert">
        <p className="state-title">{stale ? "Changed since the preview" : errTitle}</p>
        <p>
          {stale
            ? "Nothing was applied. The project changed after you reviewed this decision; review it again."
            : detail}
        </p>
        <div className="button-row">
          <button type="button" className="button primary" onClick={preview}>
            {stale ? "Refresh and review again" : "Try the preview again"}
          </button>
          <button type="button" className="button secondary" onClick={() => setPhase({ name: "idle" })}>
            Cancel
          </button>
        </div>
      </div>
    );
  }

  const shown = phase.preview;
  const busy = phase.name === "applying";
  return (
    <section
      className="decision"
      aria-labelledby={titleId}
      tabIndex={-1}
      ref={regionRef}
      data-testid="decision-preview"
    >
      <h3 id={titleId}>Review before applying: {title}</h3>
      <div className="decision-affected">{affected}</div>
      {shown.would_apply ? (
        <>
          <p className="decision-label">Protected effects (server preview):</p>
          <ul className="effects" data-testid="protected-effects">
            {shown.protected_effects.map((effect) => (
              <li key={effect}>
                {describeEffect(effect)} <span className="muted">({effect})</span>
              </li>
            ))}
          </ul>
          <RootChanges changes={shown.root_changes} />
          <p className="muted">Nothing has changed yet. Approving applies exactly this request.</p>
        </>
      ) : (
        <p className="inline-warning" role="alert">
          This request would not apply as it stands; nothing can be approved. Cancel and refresh.
        </p>
      )}
      <Details>
        <p>
          Request digest: <code>{shown.digest}</code>
        </p>
        <p>Operations: {shown.operations.join(", ")}</p>
        <p>Based on project sequence {shown.expected_seq}</p>
      </Details>
      <div className="button-row">
        <button
          type="button"
          className="button primary"
          onClick={approve}
          disabled={busy || !shown.would_apply}
          aria-busy={busy}
        >
          {busy ? "Applying…" : approveLabel}
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => setPhase({ name: "idle" })}
          disabled={busy}
        >
          Cancel
        </button>
      </div>
    </section>
  );
}
