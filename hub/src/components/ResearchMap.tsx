// The research map (A06.1, CP20.2; decision 0083): each deliverable's derived stage, the
// questions it serves and the claims answering them. Everything is derived by the core; this only
// shows it. Identifiers, digests and paths appear only inside a claim's Details (spec/06). CP20.4
// (gap audit #16): the Details show each claim's verifiables (the file and its digest, the run that
// wrote it and its receipt, the latest checks with expected, measured and tolerance), a link your
// agent placed says so, and the map shows the objective's questions, unplaced ones and the tracks.
import { useState } from "react";
import { ApiError, describeError, post, uuid4 } from "../api";
import { useHub } from "../hub";
import { Details, lineLabel } from "./ui";
import { Link } from "../router";
import { ClaimStatusCell } from "../pages/Papers";
import { stageSummary } from "../stage";
import type {
  MapAssessment,
  MapClaim,
  MapDeliverable,
  MapEvidence,
  MapQuestion,
  MapUnplacedClaim,
  ResearchMapView,
} from "../types";

// The three derived steps, then the paper's own status (spec/03 deliverable stages).
const STATUS_STEPS: { label: string; from: string[] }[] = [
  { label: "Drafting", from: ["drafting", "internal_review", "ready_for_submission", "submitted", "revision", "accepted"] },
  { label: "Internal review", from: ["internal_review", "ready_for_submission", "submitted", "revision", "accepted"] },
  { label: "Submitted", from: ["submitted", "revision", "accepted"] },
];

const HEALTH_TEXT: [keyof MapDeliverable["health"], string][] = [
  ["holds", "hold"],
  ["corrected", "corrected"],
  ["settling", "settling"],
  ["unverified", "unverified"],
  ["conflicting", "conflicting"],
  ["stale", "stale"],
];

/** Framed → Evidence mapped → Verified → Drafting → Internal review → Submitted, in words and
 * marks; the first unreached step is the next one. */
export function StageBar({
  deliverable,
  compact = false,
}: {
  deliverable: MapDeliverable;
  compact?: boolean;
}) {
  const derived = deliverable.steps.map((s) => ({ label: s.label, reached: s.reached }));
  const allDerived = derived.every((s) => s.reached);
  const status = STATUS_STEPS.map((s) => ({
    label: s.label,
    reached: allDerived && s.from.includes(deliverable.paper_status),
  }));
  const steps = [...derived, ...status];
  const next = steps.findIndex((s) => !s.reached);
  return (
    <>
    <ol className={compact ? "stage-bar compact" : "stage-bar"} aria-label="Stage" data-testid="stage-bar">
      {steps.map((s, i) => (
        <li
          key={s.label}
          className={s.reached ? "stage-step reached" : i === next ? "stage-step next" : "stage-step"}
          data-reached={s.reached ? "yes" : "no"}
        >
          <span className="stage-mark" aria-hidden="true" />
          <span className="stage-label">
            {s.label}
            <span className="visually-hidden">
              {s.reached ? " (reached)" : i === next ? " (next)" : " (not yet)"}
            </span>
          </span>
        </li>
      ))}
    </ol>
    {compact ? (
      <p className="stage-summary" aria-hidden="true" data-testid="stage-summary">
        {stageSummary(deliverable)}
      </p>
    ) : null}
    </>
  );
}

/** What keeps the paper from its next derived step, in plain words. */
export function NextStep({ deliverable }: { deliverable: MapDeliverable }) {
  const step = deliverable.steps.find((s) => s.key === deliverable.next);
  if (!step) return null;
  return (
    <div className="stage-next" data-testid="stage-next">
      <p className="lead-label">To reach “{step.label}”</p>
      <ul>
        {step.blockers.slice(0, 7).map((b, i) => (
          <li key={`${b.code}-${b.claim ?? b.question ?? i}`}>
            {b.line ? <q>{b.line}</q> : null}
            {b.line ? " · " : null}
            {b.message}
          </li>
        ))}
      </ul>
      {step.blockers.length > 7 ? (
        <p className="muted">and {step.blockers.length - 7} more.</p>
      ) : null}
    </div>
  );
}

function Health({ deliverable }: { deliverable: MapDeliverable }) {
  const parts = HEALTH_TEXT.filter(([k]) => deliverable.health[k] > 0).map(
    ([k, text]) => `${deliverable.health[k]} ${text}`,
  );
  return (
    <p className="muted health" data-testid="claim-health">
      {parts.length ? `Claims: ${parts.join(", ")}` : "No claims linked yet"}
    </p>
  );
}

/** One deliverable on the project landing (Now). */
export function DeliverableCard({ deliverable }: { deliverable: MapDeliverable }) {
  return (
    <li className="card deliverable-card" data-testid="deliverable">
      <p className="card-title">
        <Link to={`/papers/${deliverable.paper_id}`}>{deliverable.title}</Link>
      </p>
      <p data-testid="deliverable-headline">{deliverable.headline}</p>
      <StageBar deliverable={deliverable} compact />
      <Health deliverable={deliverable} />
      <Details label="What moves it forward">
        <NextStep deliverable={deliverable} />
        {deliverable.next === null ? <p>Every derived step is reached.</p> : null}
      </Details>
    </li>
  );
}

const short = (digest: string | null) => (digest ? digest.slice(0, 12) : "no digest");

function where(e: MapEvidence): string {
  const loc = e.locator;
  if (!loc) return "cited by link";
  if (loc.type === "file") return "the whole file";
  if (loc.type === "text") return lineLabel(loc.start_line, loc.end_line);
  if (loc.type === "pdf") return `page ${loc.page ?? "?"}`;
  if (loc.type === "json") return `at ${loc.pointer ?? "/"}`;
  return loc.type;
}

function EvidenceItem({ e }: { e: MapEvidence }) {
  return (
    <li data-testid="claim-evidence" data-locator={e.locator?.type ?? "none"}>
      {e.title ?? "Source"}
      {e.path ? ` · ${e.root_alias ?? ""}/${e.path}` : ""} · {where(e)}
      {e.sha256 ? ` · sha256 ${short(e.sha256)}` : ""}
      {e.byte_length !== null ? ` · ${e.byte_length} bytes` : ""}
      {e.availability && e.availability !== "retained" ? ` · ${e.availability}` : ""}
      {e.runs.length ? (
        <ul>
          {e.runs.map((r) => (
            <li key={r.receipt_version_id} data-testid="claim-run">
              Written by a run {r.reporter} reported ({r.outcome}
              {r.ended_at ? `, ended ${r.ended_at}` : ""}): <code>{r.command}</code>
              {r.producers.length ? (
                <>
                  {" "}
                  from{" "}
                  {r.producers.map((f, i) => (
                    <span key={f.version_id}>
                      {i ? ", " : ""}
                      {f.root_alias ? `${f.root_alias}/` : ""}
                      {f.path ?? f.title ?? "a file"} (sha256 {short(f.sha256)})
                    </span>
                  ))}
                </>
              ) : null}
              {" · receipt "}
              {r.receipt_title ?? "untitled"} ({r.receipt_version_id})
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

const BASIS_TEXT: Record<MapAssessment["basis"], string> = {
  reviewed: "you reviewed it",
  deterministic: "a deterministic check",
  agent_reported: "your agent reported it",
};

function Checks({ assessment }: { assessment: MapAssessment | null }) {
  if (!assessment) return <>no assessment yet</>;
  return (
    <>
      {assessment.outcome} ({BASIS_TEXT[assessment.basis]}; {assessment.assessment_id})
      {assessment.checks.length ? (
        <table className="data-table" data-testid="claim-checks">
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Result</th>
              <th scope="col">Expected</th>
              <th scope="col">Measured</th>
              <th scope="col">Tolerance</th>
            </tr>
          </thead>
          <tbody>
            {assessment.checks.map((c, i) => (
              <tr key={`${c.name}-${i}`}>
                <td>{c.name}</td>
                <td>{c.result}</td>
                <td>{c.expected ?? "-"}</td>
                <td>{c.measured ?? "-"}</td>
                <td>{c.tolerance ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <span className="muted"> · it names no checks</span>
      )}
    </>
  );
}

function PlacedByAgent({ by, what }: { by: MapClaim["linked_by"]; what: string }) {
  if (by !== "agent") return null;
  return (
    <span className="badge badge-review" data-testid="placed-by-agent" title={`Your agent placed ${what}; PrometheOS did not check it.`}>
      placed by your agent
    </span>
  );
}

function ClaimRow({ item }: { item: MapClaim }) {
  return (
    <li className="map-claim" data-testid="map-claim" data-status={item.status} data-linked-by={item.linked_by ?? "none"}>
      <p className="map-claim-line">
        {item.line} <PlacedByAgent by={item.linked_by} what="this claim under the question" />
      </p>
      {item.replaces ? <p className="muted">Replaces a superseded claim under this question.</p> : null}
      <ClaimStatusCell item={item} reason={item.reason} />
      <Details>
        <dl className="kv" data-testid="claim-details">
          <div className="kv-row">
            <dt>Claim</dt>
            <dd>
              <Link to={`/knowledge#${item.claim_id}`}>{item.claim_id}</Link> (revision {item.rev})
            </dd>
          </div>
          <div className="kv-row">
            <dt>Statement</dt>
            <dd>{item.details.statement}</dd>
          </div>
          <div className="kv-row">
            <dt>Why</dt>
            <dd>
              <ul>
                {item.causes.map((c) => (
                  <li key={`${c.code}-${c.ref ?? ""}`}>{c.message}</li>
                ))}
              </ul>
            </dd>
          </div>
          <div className="kv-row">
            <dt>Evidence</dt>
            <dd>
              {item.details.evidence.length === 0 ? (
                "none cited"
              ) : (
                <ul>
                  {item.details.evidence.map((e) => (
                    <EvidenceItem key={e.version_id} e={e} />
                  ))}
                </ul>
              )}
            </dd>
          </div>
          <div className="kv-row">
            <dt>Latest assessment</dt>
            <dd>
              <Checks assessment={item.details.assessment} />
            </dd>
          </div>
        </dl>
      </Details>
    </li>
  );
}

function QuestionBlock({ question, level = 3 }: { question: MapQuestion; level?: 3 | 4 }) {
  const Heading = level === 3 ? "h3" : "h4";
  return (
    <section className="map-question" data-testid="map-question" data-linked-by={question.linked_by ?? "none"}>
      <Heading>
        {question.title} <PlacedByAgent by={question.linked_by} what="this question here" />
      </Heading>
      {question.claims.length === 0 ? (
        <p className="muted">No claim answers this question yet.</p>
      ) : (
        <ul className="map-claims">
          {question.claims.map((c) => (
            <ClaimRow key={c.claim_id} item={c} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** A paper's stage, its questions with their claims, and what the owner is asked to confirm. */
export function PaperMap({ deliverable }: { deliverable: MapDeliverable }) {
  return (
    <section className="section" aria-labelledby="map-title" data-testid="paper-map">
      <h2 id="map-title">Questions and claims</h2>
      <p data-testid="deliverable-headline">{deliverable.headline}</p>
      <p className="muted" data-testid="map-note">
        The questions and the links under them are descriptive: recorded by you or your agent, and not
        checked by PrometheOS. A claim's basis says who checked the claim, not who placed it here.
      </p>
      <StageBar deliverable={deliverable} />
      <NextStep deliverable={deliverable} />
      {deliverable.to_confirm.length > 0 ? (
        <div className="stage-confirm" data-testid="to-confirm">
          <p className="lead-label">Resting on your agent's check alone: confirm or ask for a review</p>
          <ul>
            {deliverable.to_confirm.map((c) => (
              <li key={c.claim_id}>
                <q>{c.line}</q>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {deliverable.questions.map((q) => (
        <QuestionBlock key={q.entry_id} question={q} />
      ))}
      {deliverable.other_claims.length > 0 ? (
        <section className="map-question" data-testid="map-unplaced">
          <h3>Claims not yet under a question</h3>
          <ul className="map-claims">
            {deliverable.other_claims.map((c) => (
              <ClaimRow key={c.claim_id} item={c} />
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}

/** The map beyond the deliverables: the questions the objective itself serves, questions placed
 * nowhere yet, and the tracks with what draws on them. */
export function MapOverview({ map, onChanged }: { map: ResearchMapView; onChanged?: () => void }) {
  const { objective, unplaced_questions: unplaced, tracks } = map;
  const loose = map.unplaced_claims ?? [];
  if (!objective.questions.length && !unplaced.length && !tracks.length && !loose.length) return null;
  return (
    <div className="map-overview" data-testid="map-overview">
      {loose.length ? <UnplacedClaims claims={loose} seq={map.project_seq} onChanged={onChanged} /> : null}
      {objective.questions.length ? (
        <section data-testid="map-objective-questions" aria-labelledby="map-objective-title">
          <h3 id="map-objective-title">Questions for the objective</h3>
          {objective.questions.map((q) => (
            <QuestionBlock key={q.entry_id} question={q} level={4} />
          ))}
        </section>
      ) : null}
      {unplaced.length ? (
        <section data-testid="map-unplaced-questions" aria-labelledby="map-unplaced-title">
          <h3 id="map-unplaced-title">Questions not placed yet</h3>
          <p className="muted">They address no paper and not the objective.</p>
          {unplaced.map((q) => (
            <QuestionBlock key={q.entry_id} question={q} level={4} />
          ))}
        </section>
      ) : null}
      {tracks.length ? (
        <section data-testid="map-tracks" aria-labelledby="map-tracks-title">
          <h3 id="map-tracks-title">Tracks</h3>
          <ul className="plain-list">
            {tracks.map((t) => (
              <li key={t.entry_id} data-testid="map-track">
                <strong>{t.title}</strong>
                <span className="muted">
                  {" · "}
                  {t.questions} question{t.questions === 1 ? "" : "s"}, {t.claims} claim{t.claims === 1 ? "" : "s"} in use
                  draw on it
                  {t.claims_by_path ? (
                    <span data-testid="map-track-by-path">
                      {"; "}
                      {t.claims_by_path} more by file location
                      {t.folders?.length ? ` (${t.folders.join(", ")})` : ""}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/** Claims in use that answer no question (decision 0109), at most five shown. When the
 * intelligence layer's two models agree on a question, it is proposed with the claim's own words
 * that tie them; placing it is one act of yours (an ``answers`` link), and nothing was written by
 * a model. Without a proposal, your agent can place it (its link then reads as placed by it). */
function UnplacedClaims({
  claims,
  seq,
  onChanged,
}: {
  claims: MapUnplacedClaim[];
  seq: number;
  onChanged?: () => void;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? claims : claims.slice(0, 5);
  return (
    <section data-testid="map-unplaced-claims" aria-labelledby="map-unplaced-claims-title">
      <h3 id="map-unplaced-claims-title">Claims not under a question · {claims.length}</h3>
      <ul className="plain-list">
        {shown.map((c) => (
          <UnplacedClaim key={c.claim_id} claim={c} seq={seq} onChanged={onChanged} />
        ))}
      </ul>
      {claims.length > shown.length ? (
        <button type="button" className="link-button" onClick={() => setAll(true)}>
          Show all {claims.length}
        </button>
      ) : null}
    </section>
  );
}

function UnplacedClaim({
  claim,
  seq,
  onChanged,
}: {
  claim: MapUnplacedClaim;
  seq: number;
  onChanged?: () => void;
}) {
  const { project, refresh } = useHub();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const s = claim.suggestion;
  const place = async () => {
    if (!project || !s?.question_id || !s.question_rev) return;
    setBusy(true);
    setError(null);
    try {
      await post("/transactions", {
        schema_version: 1,
        project_id: project.project_id,
        request_id: uuid4(),
        // The map's own sequence: the write fails if anything changed since this proposal and
        // these revisions were read (review map-ai-spread-a R1-F2).
        expected_seq: seq,
        operations: [
          {
            op: "relation_add",
            source_ref: { id: claim.claim_id, rev: claim.rev },
            target_ref: { id: s.question_id, rev: s.question_rev },
            relation: "answers",
          },
        ],
      });
      await refresh().catch(() => undefined);
      onChanged?.();
    } catch (err) {
      if (err instanceof ApiError && err.kind === "stale") {
        // Review map-ai-spread-a R2-F2: the project changed since the map was read; show the
        // current map (and proposal) instead of a dead end.
        setError("The project changed since this was shown; the map was reloaded. Check it again.");
        onChanged?.();
      } else {
        setError(describeError(err).title);
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="row unplaced-claim" data-testid="unplaced-claim">
      <span>{claim.line}</span> <ClaimStatusCell item={claim} reason={claim.reason} />
      {s?.agree ? (
        <div className="layer-suggestion" data-testid="claim-suggestion">
          <span className="muted">The layer proposes: </span>
          <strong>{s.question_title}</strong>
          <span className="muted">
            {" "}
            (two models agree{s.executors?.includes("cassette") ? ", from a recording" : ""}
            {s.hint_only ? "; a hint only: a model outside the evaluated list answered" : ""}; it ties on “{s.quote}”).
          </span>{" "}
          <button
            type="button"
            className="button small"
            disabled={busy}
            aria-label={`Place it under ${s.question_title ?? "this question"}`}
            onClick={() => void place()}
          >
            Place it there
          </button>
          {error ? <span role="alert"> {error}</span> : null}
        </div>
      ) : s ? (
        <p className="muted small-print">The layer's models disagree on its question, so there is no proposal.</p>
      ) : null}
    </li>
  );
}
