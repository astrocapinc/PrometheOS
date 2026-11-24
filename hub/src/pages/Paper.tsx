// One paper: readiness findings (pure check), the manuscript and its anchors, venue
// requirement rows with their exact source lines, and reviewer comments. Clearing an
// anchor's review flag is an owner decision (preview -> approve); recording a requirement
// check is an ordinary revision of the paper. Nothing here submits or compiles anything.
import { useId, useState, type FormEvent } from "react";
import { describeError, get, post, uuid4 } from "../api";
import { DecisionReview, type DecisionRequest } from "../components/DecisionReview";
import { PaperTools } from "../components/parity";
import {
  Details,
  ErrorState,
  KeyValues,
  Loading,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  formatDate,
  humanizeIds,
  lineLabel,
} from "../components/ui";
import { useKindList, useRecords } from "../data";
import { useHub, useQuery, type QueryState } from "../hub";
import { Link } from "../router";
import { PaperMap } from "../components/ResearchMap";
import { ClaimStatusCell } from "./Papers";
import type {
  ClaimStatusView,
  AnchorBody,
  ClaimBody,
  Envelope,
  PaperBody,
  PaperCheck,
  PaperFinding,
  PaperOverview,
  ProjectInfo,
  Requirement,
  MapDeliverable,
  NumberTrace,
  PaperNumbers,
  ResearchMapView,
  SourceBody,
  VersionBody,
} from "../types";

const STATUS_TEXT: Record<string, string> = {
  current: "Current",
  needs_review: "Needs review",
  resolved: "Resolved",
};

const APPLIES_TEXT: Record<Requirement["applicability"], string> = {
  unknown: "Unknown",
  applicable: "Applies",
  not_applicable: "Does not apply",
};
const REQUIREMENT_TEXT: Record<Requirement["status"], string> = {
  unknown: "Not checked",
  todo: "To do",
  met: "Met",
  not_applicable: "Not applicable",
};

function nowStamp(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

async function freshSeq(): Promise<number> {
  return (await get<ProjectInfo>("/project")).project_seq;
}

// Decision 0109: an empty section is one word in a "Nothing yet" line, never a box; opening it
// shows the section with its forms. Keys name the sections that can be empty.
type Folded = "anchors" | "files" | "venue" | "reviews";
const FOLDED_LABEL: Record<Folded, string> = {
  anchors: "manuscript anchors",
  files: "files used",
  venue: "venue requirements",
  reviews: "reviewer comments",
};

export function PaperPage({ id }: { id: string }) {
  const [opened, setOpened] = useState<Set<Folded>>(() => new Set());
  const open = (key: Folded) => setOpened((prev) => new Set(prev).add(key));
  const overview = useQuery<PaperOverview>(`/papers/${encodeURIComponent(id)}`);
  const check = useQuery<PaperCheck>(`/papers/${encodeURIComponent(id)}/check`);
  const map = useQuery<ResearchMapView>(`/knowledge/research-map?paper_id=${encodeURIComponent(id)}`);
  const sources = useKindList<SourceBody>("source");
  const claimIds = new Set<string>();
  for (const ref of overview.data?.paper.body.claim_refs ?? []) claimIds.add(ref.id);
  for (const a of overview.data?.anchors ?? []) for (const ref of a.body.claim_refs) claimIds.add(ref.id);
  const claims = useRecords([...claimIds].sort());
  // Replacement claims (a superseded claim names its successor) so findings can quote them.
  const replacementIds = new Set<string>();
  for (const c of claims.records.values()) {
    const next = (c.body as unknown as ClaimBody).superseded_by?.id;
    if (next && !claimIds.has(next)) replacementIds.add(next);
  }
  const replacements = useRecords([...replacementIds].sort());
  const sourceTitle = (sid: string) =>
    sources.data?.items.find((s) => s.id === sid)?.body.title ?? "Source";

  return (
    <QueryView query={overview} loadingLabel="Loading paper">
      {(data) => {
        const paper = data.paper;
        const latest = data.manuscript_versions[0] ?? null;
        const claim = (cid: string) =>
          (claims.records.get(cid) ?? replacements.records.get(cid)) as
            | Envelope<ClaimBody>
            | undefined;
        return (
          <article aria-labelledby="page-title">
            <PageHeading title={paper.body.title} lead={paper.body.objective} />
            {latest && latest.body.mime === "application/x-tex" ? (
              <p>
                <Link to={`/papers/${paper.id}/workspace`} className="button secondary small">
                  Open the LaTeX workspace
                </Link>
              </p>
            ) : null}
            <KeyValues
              rows={[
                ["Status", <StateBadge key="s" state={paper.body.status} />],
                ["Deadline", paper.body.deadline ? formatDate(paper.body.deadline) : "none recorded"],
                [
                  "Venue",
                  paper.body.venue_source_refs.length === 0 ? (
                    <span key="v" data-testid="venue-unknown">
                      Unknown: no dated venue guidance is recorded
                    </span>
                  ) : (
                    <span key="v">
                      {paper.body.venue_source_refs.length} dated venue source(s) recorded
                    </span>
                  ),
                ],
                [
                  "Manuscript",
                  latest ? (
                    <span key="m">
                      Latest version captured {formatDate(latest.body.captured_at)} ·{" "}
                      <StateBadge state={latest.body.availability} />{" "}
                      {latest.body.mime === "application/pdf" ? (
                        <a
                          href={`/api/v1/versions/${encodeURIComponent(latest.id)}/content`}
                          target="_blank"
                          rel="noopener noreferrer"
                          data-testid="open-pdf"
                        >
                          Open the PDF in a new tab
                        </a>
                      ) : (
                        <SourceButton
                          spec={{
                            versionId: latest.id,
                            title: sourceTitle(latest.body.source_id),
                            citedBy: "Manuscript (latest version)",
                          }}
                        />
                      )}
                    </span>
                  ) : (
                    "no manuscript source recorded"
                  ),
                ],
              ]}
            />
            {map.error ? (
              <ErrorState error={map.error} />
            ) : map.data?.deliverables[0] ? (
              <PaperMap deliverable={map.data.deliverables[0]} />
            ) : null}
            <Readiness
              paperId={paper.id}
              check={check}
              claim={claim}
              shown={mapped(map.data?.deliverables[0])}
            />
            {latest ? <Numbers paperId={paper.id} /> : null}
            {(() => {
              const empty: Record<Folded, boolean> = {
                anchors: data.anchors.length === 0,
                files: data.used_versions.length === 0,
                venue: paper.body.venue_source_refs.length === 0 && paper.body.requirements.length === 0,
                reviews: paper.body.review_rounds.length === 0,
              };
              const folded = (Object.keys(FOLDED_LABEL) as Folded[]).filter((k) => empty[k] && !opened.has(k));
              return folded.length ? (
                <p className="nothing-yet" data-testid="paper-nothing-yet">
                  <span className="muted">Nothing yet:</span>{" "}
                  {folded.map((k, i) => (
                    <span key={k}>
                      {i ? ", " : null}
                      <button type="button" className="link-button" onClick={() => open(k)}>
                        {FOLDED_LABEL[k]}
                      </button>
                    </span>
                  ))}
                  <span className="muted">. Open one to add it.</span>
                </p>
              ) : null;
            })()}
            {data.anchors.length || opened.has("anchors") ? (
            <Anchors
              data={data}
              claim={claim}
              sourceTitle={sourceTitle}
              onChanged={() => {
                overview.reload();
                check.reload();
              }}
            />
            ) : null}
            {data.used_versions.length || opened.has("files") ? (
              <UsedFiles versions={data.used_versions} sourceTitle={sourceTitle} />
            ) : null}
            {paper.body.venue_source_refs.length || paper.body.requirements.length || opened.has("venue") ? (
            <Requirements
              paper={paper}
              latestId={latest?.id ?? null}
              latestSource={latest?.body.source_id ?? null}
              sourceTitle={sourceTitle}
              onChanged={() => {
                overview.reload();
                check.reload();
              }}
            />
            ) : null}
            {paper.body.review_rounds.length || opened.has("reviews") ? (
              <ReviewRounds paper={paper} sourceTitle={sourceTitle} />
            ) : null}
            <details className="section fold" open={latest !== undefined && latest !== null}>
              <summary>
                <h2 id="bundle-title">Local bundle and handoff</h2>
              </summary>
              <p className="muted">
                Nothing here is uploaded. A bundle is the manuscript plus the retained files it uses, with a manifest
                of exact versions; a hand-off copies reviewed files into your own folder (for example a Git or
                Overleaf-Git clone) and never deletes files, runs Git or contacts a remote.
              </p>
              <PaperTools
                paperId={paper.id}
                venueVersionIds={paper.body.venue_source_refs.map((r) => r.id)}
              />
            </details>
            <Details>
              <KeyValues
                rows={[
                  ["Paper", <code key="p">{paper.id}</code>],
                  ["Revision", String(paper.rev)],
                ]}
              />
            </Details>
          </article>
        );
      }}
    </QueryView>
  );
}

// ----- numbers (decision 0111) ------------------------------------------------------------

const TRACE_TEXT: Record<NumberTrace, string> = {
  run: "written by a recorded run",
  run_inputs_changed: "written by a run whose inputs changed since",
  file_only: "no recorded run wrote it",
};
const SHOWN_TYPED = 5;

/** Where every number in the manuscript comes from: one headline, the numbers typed by hand
 * (the ones nothing traces) and, folded, the generated files with the runs that wrote them. */
function Numbers({ paperId }: { paperId: string }) {
  const numbers = useQuery<PaperNumbers>(`/papers/${encodeURIComponent(paperId)}/numbers`);
  const [all, setAll] = useState(false);
  return (
    <section aria-labelledby="numbers-title" className="section" data-testid="paper-numbers">
      <h2 id="numbers-title">Numbers in the manuscript</h2>
      <QueryView query={numbers} loadingLabel="Tracing the numbers">
        {(n) => {
          const typed = n.numbers.filter((r) => r.kind === "typed");
          const c = n.counts;
          const unchecked = (c.formula ?? 0) + (c.reference ?? 0) + (c.unit ?? 0) + (c.year ?? 0);
          return (
            <>
              <p data-testid="numbers-headline">{n.headline}</p>
              {typed.length ? (
                <>
                  <h3>Typed by hand</h3>
                  <ul className="number-rows" data-testid="numbers-typed">
                    {(all ? typed : typed.slice(0, SHOWN_TYPED)).map((r, i) => (
                      <li key={`${i}:${r.path}:${r.line}:${r.text}`}>
                        <code>{r.text}</code>{" "}
                        <span className="muted">
                          {r.path?.split("/").pop()}:{r.line}
                        </span>{" "}
                        <span className="number-context">{r.context}</span>
                      </li>
                    ))}
                  </ul>
                  {typed.length > SHOWN_TYPED && !all ? (
                    <button type="button" className="link-button" onClick={() => setAll(true)}>
                      Show all {typed.length}
                    </button>
                  ) : null}
                  {all && n.truncated ? (
                    <p className="muted" data-testid="numbers-truncated">
                      The list stops at {typed.length}; the counts above include every number.
                    </p>
                  ) : null}
                  <p className="muted">
                    Write each as a macro from the generator script, or bind it to a claim, so it is traced.
                  </p>
                </>
              ) : null}
              {n.inputs_not_recorded.length ? (
                <p data-testid="numbers-unrecorded">
                  Not recorded, so not checked: {n.inputs_not_recorded.join(", ")}.
                </p>
              ) : null}
              {n.inputs_skipped.length ? (
                <p data-testid="numbers-skipped">
                  Past the file limit, so not checked: {n.inputs_skipped.join(", ")}.
                </p>
              ) : null}
              {n.macro_files.length || unchecked ? (
                <details className="fold">
                  <summary>
                    {n.macro_files.length} generated file(s) · {unchecked} number(s) not checked
                  </summary>
                  <ul className="number-rows">
                    {n.macro_files.map((f) => (
                      <li key={f.version_id} data-trace={f.trace}>
                        <code>{f.path}</code>{" "}
                        <span className="muted">
                          {f.role === "macros" ? `${f.used} of ${f.defined} macros used` : "generated table"} ·{" "}
                          {TRACE_TEXT[f.trace]}
                          {f.runs[0] ? ` (${f.runs[0].receipt_title})` : ""}
                        </span>
                        {f.runs.flatMap((r) =>
                          r.inputs_changed.map((ch) => ({ ...ch, key: `${r.receipt_version_id}:${ch.read_version_id}` })),
                        ).map((ch) => (
                          <div key={ch.key} className="muted">
                            Changed since: <code>{ch.path}</code>
                          </div>
                        ))}
                      </li>
                    ))}
                  </ul>
                  <p className="muted">
                    Not checked: {c.formula ?? 0} in formulas, {c.reference ?? 0} in names and cross-references,{" "}
                    {c.unit ?? 0} inside units, {c.year ?? 0} years. {n.limitations.join(" ")}
                  </p>
                </details>
              ) : null}
            </>
          );
        }}
      </QueryView>
    </section>
  );
}

// ----- readiness ------------------------------------------------------------------------

/** The claims "Questions and claims" already shows with their status (A06.1, CP20.2). */
function mapped(d: MapDeliverable | undefined): Set<string> {
  const ids = new Set<string>();
  for (const q of d?.questions ?? []) for (const c of q.claims) ids.add(c.claim_id);
  for (const c of d?.other_claims ?? []) ids.add(c.claim_id);
  return ids;
}

function Readiness({
  paperId,
  check,
  claim,
  shown,
}: {
  paperId: string;
  check: QueryState<PaperCheck>;
  claim: (id: string) => Envelope<ClaimBody> | undefined;
  shown: Set<string>;
}) {
  const { announce, refresh } = useHub();
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  // A06.1 (CP20.1): each claim finding shows the claim's derived status, not only its state.
  const statuses = useQuery<ClaimStatusView>(`/knowledge/claim-status?paper_id=${encodeURIComponent(paperId)}`);
  const statusOf = new Map((statuses.data?.claims ?? []).map((s) => [s.claim_id, s]));
  const save = async () => {
    setError(null);
    try {
      await post(`/papers/${encodeURIComponent(paperId)}/check/save`, { request_id: uuid4() });
      setSaved("Saved these findings as a notebook entry.");
      announce("Saved to the notebook.");
      await refresh();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section aria-labelledby="ready-title" className="section">
      <h2 id="ready-title">Readiness</h2>
      <QueryView query={check} loadingLabel="Checking readiness">
        {(report) => {
          // A claim already listed under "Questions and claims" is not repeated here (CP20.2).
          const isShown = (f: PaperFinding) =>
            f.code === "claim_without_support" && shown.has(f.record_ids[f.record_ids.length - 1] ?? "");
          const findings = report.findings.filter((f) => !isShown(f));
          const folded = report.findings.length - findings.length;
          return (
          <>
            {folded > 0 ? (
              <p className="muted" data-testid="findings-folded">
                {folded} claim{folded === 1 ? " is" : "s are"} not yet supported: see each status under
                Questions and claims.
              </p>
            ) : null}
            {findings.length === 0 && folded === 0 ? (
              <p className="success">No open readiness findings.</p>
            ) : findings.length === 0 ? null : (
              <ul className="plain-list findings" data-testid="findings">
                {findings.map((f, i) => (
                  <li key={i} data-code={f.code} data-testid="finding">
                    {f.code === "claim_without_support" && statusOf.has(f.record_ids[f.record_ids.length - 1] ?? "") ? (
                      <>
                        {`Claim “${claim(f.record_ids[f.record_ids.length - 1]!)?.body.summary ?? claim(f.record_ids[f.record_ids.length - 1]!)?.body.statement ?? "a claim"}”: `}
                        <ClaimStatusCell item={statusOf.get(f.record_ids[f.record_ids.length - 1]!)!} />
                      </>
                    ) : (
                      describeFinding(f, claim)
                    )}
                  </li>
                ))}
              </ul>
            )}
            {report.hidden_references > 0 ? (
              <p className="muted">Some referenced records are not visible in this view.</p>
            ) : null}
            <details className="details limitations">
              <summary>What this check does not do</summary>
              <ul className="plain-list details-body">
                {report.limitations.map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </details>
            <button type="button" className="button secondary small" onClick={() => void save()}>
              Save this check to the notebook
            </button>
            {saved ? (
              <p className="info" data-testid="check-saved">
                {saved}
              </p>
            ) : null}
            {error ? <ErrorState error={error} /> : null}
          </>
          );
        }}
      </QueryView>
    </section>
  );
}

function describeFinding(
  f: PaperFinding,
  claim: (id: string) => Envelope<ClaimBody> | undefined,
): string {
  const d = f.detail;
  const statement = (cid: string | undefined) =>
    cid ? `“${claim(cid)?.body.statement ?? "a claim"}”` : "a claim";
  const last = f.record_ids[f.record_ids.length - 1];
  switch (f.code) {
    case "venue_unknown":
      return "The target venue is unknown: import the venue's own dated guidance before checking requirements.";
    case "requirement_unchecked":
      return `Venue requirement ${String(d.requirement)} is ${
        d.applicability === "unknown" ? "not yet assessed" : "still to do"
      }.`;
    case "claim_without_support":
      return `Claim ${statement(last)} is ${String(d.state)}: no recorded support within its scope yet.`;
    case "claim_not_usable":
      return `Claim ${statement(last)} is ${String(d.status)}: do not quote it until it is checked again.`;
    case "claim_agent_reported":
      return `Claim ${statement(last)} holds on support an agent reported: confirm it yourself before relying on it.`;
    case "claim_corrected_in_use":
      return `Claim ${statement(last)} is ${String(d.state)} but is still used by ${
        f.record_ids[0]?.startsWith("anchor_") ? "a manuscript anchor" : "this paper"
      }${d.superseded_by ? `; it was replaced by ${statement(String(d.superseded_by))}` : ""}.`;
    case "anchor_needs_review":
      return `A manuscript anchor needs review: ${humanizeIds(String(d.reason ?? ""))}`;
    case "anchor_stale_version":
      return "A manuscript anchor is still bound to an older manuscript version.";
    case "anchor_without_grounding":
      return "A manuscript anchor cites no claim and no evidence.";
    case "artifact_unavailable":
      return "A cited file's retained bytes are missing or corrupt.";
    case "artifact_reference_only":
      return "A cited file is referenced but not retained in this store (it cannot go into a bundle).";
    case "manuscript_unavailable":
      return `The manuscript is not available: ${String(d.reason ?? "")}.`;
    case "review_item_open":
      return `Reviewer comment ${String(d.round)}/${String(d.item)} is ${String(d.status)}, not resolved.`;
    default:
      return humanizeIds(`${f.code}: ${f.record_ids.join(", ")}`);
  }
}

// ----- anchors ------------------------------------------------------------------------------

function Anchors({
  data,
  claim,
  sourceTitle,
  onChanged,
}: {
  data: PaperOverview;
  claim: (id: string) => Envelope<ClaimBody> | undefined;
  sourceTitle: (sid: string) => string;
  onChanged: () => void;
}) {
  const latestId = data.manuscript_versions[0]?.id ?? null;
  const versionSource = (vid: string) =>
    data.manuscript_versions.find((v) => v.id === vid)?.body.source_id ?? "";
  return (
    <section aria-labelledby="anchors-title" className="section">
      <h2 id="anchors-title">Manuscript anchors</h2>
      {data.anchors.length === 0 ? (
        <p className="state state-empty">No sentences of the manuscript are anchored to claims yet.</p>
      ) : (
        <ul className="card-list" data-testid="anchors">
          {data.anchors.map((a) => (
            <AnchorCard
              key={a.id}
              anchor={a}
              claim={claim}
              older={latestId !== null && a.body.manuscript_version_id !== latestId}
              title={sourceTitle(versionSource(a.body.manuscript_version_id))}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function AnchorCard({
  anchor,
  claim,
  older,
  title,
  onChanged,
}: {
  anchor: Envelope<AnchorBody>;
  claim: (id: string) => Envelope<ClaimBody> | undefined;
  older: boolean;
  title: string;
  onChanged: () => void;
}) {
  const b = anchor.body;
  const loc = b.locator;
  const where = loc.type === "pdf" ? `page ${String(loc.page)}` : lineLabel(loc.start_line, loc.end_line);
  return (
    <li className="card" id={anchor.id} data-testid="anchor-card" data-status={b.status}>
      <div className="card-head">
        <span className="card-title">Manuscript {where}</span>
        <span className={`badge badge-${b.status === "needs_review" ? "review" : b.status}`} data-state={b.status}>
          {STATUS_TEXT[b.status] ?? b.status}
        </span>
      </div>
      {loc.type === "text" ? (
        <SourceButton
          spec={{
            versionId: b.manuscript_version_id,
            title: title || "Manuscript",
            startLine: loc.start_line,
            endLine: loc.end_line,
            citedBy: "Anchored manuscript text",
          }}
        />
      ) : null}
      {older ? <p className="muted">Bound to an older manuscript version.</p> : null}
      {b.reason ? (
        <p data-testid="anchor-reason">
          <span className="muted">Why: </span>
          {humanizeIds(b.reason)}
        </p>
      ) : null}
      <ul className="plain-list">
        {b.claim_refs.map((ref) => {
          const c = claim(ref.id);
          return (
            <li key={ref.id}>
              Uses claim{" "}
              <Link to={`/knowledge#${ref.id}`}>“{c?.body.statement ?? "loading…"}”</Link>{" "}
              {c ? <StateBadge state={c.body.state} /> : null}
            </li>
          );
        })}
      </ul>
      {b.status === "needs_review" ? (
        <AnchorReview anchor={anchor} claim={claim} onApplied={onChanged} />
      ) : null}
    </li>
  );
}

function AnchorReview({
  anchor,
  claim,
  onApplied,
}: {
  anchor: Envelope<AnchorBody>;
  claim: (id: string) => Envelope<ClaimBody> | undefined;
  onApplied: () => void;
}) {
  const { project } = useHub();
  const ids = useId();
  const replacements = anchor.body.claim_refs
    .map((ref) => claim(ref.id))
    .filter((c): c is Envelope<ClaimBody> => !!c && !!c.body.superseded_by);
  // Until the researcher picks, default to the replacement claim when one exists (claims
  // load after the first render, so the default is derived, not frozen in state).
  const [picked, setChoice] = useState<"rebind" | "current" | "resolved" | null>(null);
  const choice = picked ?? (replacements.length > 0 ? "rebind" : "current");
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const replacementIds = replacements.map((c) => c.body.superseded_by?.id ?? "");
  const replacementRecords = useRecords(replacementIds);
  const replacementNames = replacementIds.map((rid) => {
    const body = replacementRecords.records.get(rid)?.body as ClaimBody | undefined;
    return body ? `“${body.statement}”` : "the replacement claim";
  });

  const build = async (): Promise<DecisionRequest> => {
    if (!reason.trim()) {
      setFieldError("Say why: the reason is recorded with the decision.");
      throw new Error("A reason is required.");
    }
    setFieldError(null);
    const operations: Record<string, unknown>[] = [];
    let rev = anchor.rev;
    if (choice === "rebind") {
      const refs = anchor.body.claim_refs.map((ref) => {
        const c = claim(ref.id);
        const next = c?.body.superseded_by?.id;
        const replacement = next ? replacementRecords.records.get(next) : undefined;
        return replacement ? { id: replacement.id, rev: replacement.rev } : ref;
      });
      operations.push({
        op: "revise",
        id: anchor.id,
        base_rev: rev,
        sensitivity: anchor.sensitivity,
        occurred_at: anchor.occurred_at,
        body: { ...anchor.body, claim_refs: refs },
      });
      rev += 1;
    }
    operations.push({
      op: "transition",
      id: anchor.id,
      base_rev: rev,
      to: choice === "resolved" ? "resolved" : "current",
      reason: reason.trim(),
      evidence: [],
    });
    return {
      schema_version: 1,
      project_id: project?.project_id,
      request_id: uuid4(),
      expected_seq: await freshSeq(),
      operations,
    };
  };

  return (
    <div className="anchor-review" data-testid="anchor-review">
      <fieldset className="type-choice">
        <legend>Review this anchor</legend>
        {replacements.length > 0 ? (
          <label>
            <input
              type="radio"
              name={`${ids}-choice`}
              checked={choice === "rebind"}
              onChange={() => setChoice("rebind")}
            />{" "}
            Use the replacement claim
          </label>
        ) : null}
        <label>
          <input
            type="radio"
            name={`${ids}-choice`}
            checked={choice === "current"}
            onChange={() => setChoice("current")}
          />{" "}
          Still valid as written
        </label>
        <label>
          <input
            type="radio"
            name={`${ids}-choice`}
            checked={choice === "resolved"}
            onChange={() => setChoice("resolved")}
          />{" "}
          Resolved (sentence rewritten or removed)
        </label>
      </fieldset>
      <div className="field">
        <label htmlFor={`${ids}-reason`}>Reason (recorded)</label>
        <textarea
          id={`${ids}-reason`}
          rows={2}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          aria-invalid={fieldError ? true : undefined}
        />
        {fieldError ? (
          <p className="field-error" role="alert">
            {fieldError}
          </p>
        ) : null}
      </div>
      <DecisionReview
        startLabel="Preview this review decision"
        title="clear this anchor's review flag"
        approveLabel="Approve anchor review"
        affected={
          <ul>
            {choice === "rebind" ? (
              <li>The anchor will use {replacementNames.join(", ")} instead.</li>
            ) : null}
            <li>The anchor will be marked {choice === "resolved" ? "Resolved" : "Current"}.</li>
          </ul>
        }
        build={build}
        onApplied={onApplied}
      />
    </div>
  );
}

// ----- files used ---------------------------------------------------------------------------

const BROWSER_VIEWABLE = new Set([
  "application/pdf",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

function UsedFiles({
  versions,
  sourceTitle,
}: {
  versions: Envelope<VersionBody>[];
  sourceTitle: (sid: string) => string;
}) {
  return (
    <section aria-labelledby="files-title" className="section">
      <h2 id="files-title">Files used by this paper</h2>
      {versions.length === 0 ? (
        <p className="state state-empty">
          No figures, tables or compiled outputs are registered as used by this paper.
        </p>
      ) : (
        <ul className="plain-list" data-testid="used-files">
          {versions.map((v) => (
            <li key={v.id} className="row">
              <span>{sourceTitle(v.body.source_id)}</span>{" "}
              <span className="muted">
                ({v.body.mime}, captured {formatDate(v.body.captured_at)})
              </span>{" "}
              {v.body.availability !== "retained" ? (
                <StateBadge state={v.body.availability} />
              ) : BROWSER_VIEWABLE.has(v.body.mime) ? (
                <a
                  href={`/api/v1/versions/${encodeURIComponent(v.id)}/content`}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="open-file"
                >
                  Open in a new tab
                </a>
              ) : v.body.mime.startsWith("text/") ? (
                <SourceButton spec={{ versionId: v.id, title: sourceTitle(v.body.source_id) }} />
              ) : (
                <span className="muted">open it in an existing tool</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ----- venue requirements -------------------------------------------------------------

function Requirements({
  paper,
  latestId,
  latestSource,
  sourceTitle,
  onChanged,
}: {
  paper: Envelope<PaperBody>;
  latestId: string | null;
  latestSource: string | null;
  sourceTitle: (sid: string) => string;
  onChanged: () => void;
}) {
  const rows = paper.body.requirements;
  return (
    <section aria-labelledby="req-title" className="section">
      <h2 id="req-title">Venue requirements</h2>
      {paper.body.venue_source_refs.length === 0 ? (
        <p className="state state-empty">
          Venue unknown. Requirements come only from the venue's own dated guidance, imported as a
          source; nothing is assumed from a generic checklist.
        </p>
      ) : rows.length === 0 ? (
        <p className="state state-empty">No requirement rows recorded from the venue guidance.</p>
      ) : (
        <div className="table-wrap">
          <table className="evidence-table" data-testid="requirements">
            <caption className="visually-hidden">Venue requirements, their source lines and checks</caption>
            <thead>
              <tr>
                <th scope="col">Requirement</th>
                <th scope="col">Source</th>
                <th scope="col">Applies?</th>
                <th scope="col">Status</th>
                <th scope="col">Record a check</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} data-testid="requirement-row" data-status={row.status}>
                  <th scope="row">{row.text}</th>
                  <td>
                    <SourceButton
                      spec={{
                        versionId: row.source_ref.version_id,
                        title: sourceTitle(row.source_ref.source_id),
                        startLine: row.source_ref.locator.start_line,
                        endLine: row.source_ref.locator.end_line,
                        citedBy: `Requirement ${row.id}`,
                      }}
                    />
                  </td>
                  <td>{APPLIES_TEXT[row.applicability]}</td>
                  <td>
                    {REQUIREMENT_TEXT[row.status]}
                    {row.last_checked_at ? (
                      <span className="muted"> · checked {formatDate(row.last_checked_at)}</span>
                    ) : null}
                  </td>
                  <td>
                    <RequirementForm
                      paper={paper}
                      row={row}
                      latestId={latestId}
                      latestSource={latestSource}
                      onSaved={onChanged}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RequirementForm({
  paper,
  row,
  latestId,
  latestSource,
  onSaved,
}: {
  paper: Envelope<PaperBody>;
  row: Requirement;
  latestId: string | null;
  latestSource: string | null;
  onSaved: () => void;
}) {
  const { project, announce, refresh } = useHub();
  const ids = useId();
  const [status, setStatus] = useState<"todo" | "met" | "not_applicable">("todo");
  const [line, setLine] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    const evidenceLine = Number.parseInt(line, 10);
    if (status === "met" && (!latestId || !latestSource || !(evidenceLine >= 1))) {
      setFieldError("Met needs evidence: the manuscript line that satisfies it.");
      return;
    }
    setFieldError(null);
    const updated: Requirement = {
      ...row,
      applicability: status === "not_applicable" ? "not_applicable" : "applicable",
      status,
      last_checked_at: nowStamp(),
      evidence:
        status === "met" && latestId && latestSource
          ? [
              {
                source_id: latestSource,
                version_id: latestId,
                locator: { type: "text", start_line: evidenceLine, end_line: evidenceLine },
                quote_sha256: null,
              },
            ]
          : [],
    };
    setBusy(true);
    try {
      await post("/transactions", {
        schema_version: 1,
        project_id: project?.project_id,
        request_id: uuid4(),
        expected_seq: await freshSeq(),
        operations: [
          {
            op: "revise",
            id: paper.id,
            base_rev: paper.rev,
            sensitivity: paper.sensitivity,
            occurred_at: paper.occurred_at,
            body: {
              ...paper.body,
              requirements: paper.body.requirements.map((r) => (r.id === row.id ? updated : r)),
            },
          },
        ],
      });
      announce(`Recorded the check of requirement ${row.id}.`);
      await refresh();
      onSaved();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="requirement-form" noValidate>
      <div className="field">
        <label htmlFor={`${ids}-status`}>Status of {row.id}</label>
        <select
          id={`${ids}-status`}
          value={status}
          onChange={(e) => setStatus(e.target.value as "todo" | "met" | "not_applicable")}
        >
          <option value="todo">Applies, still to do</option>
          <option value="met">Applies, met</option>
          <option value="not_applicable">Does not apply</option>
        </select>
      </div>
      {status === "met" ? (
        <div className="field">
          <label htmlFor={`${ids}-line`}>Manuscript line showing it is met</label>
          <input
            id={`${ids}-line`}
            inputMode="numeric"
            value={line}
            onChange={(e) => setLine(e.target.value)}
            aria-invalid={fieldError ? true : undefined}
          />
        </div>
      ) : null}
      {fieldError ? (
        <p className="field-error" role="alert">
          {fieldError}
        </p>
      ) : null}
      <button type="submit" className="button secondary small" disabled={busy} aria-busy={busy}>
        {busy ? "Saving…" : "Record check"}
      </button>
      {error ? <ErrorState error={error} /> : null}
      {busy ? <Loading label="Saving" /> : null}
    </form>
  );
}

// ----- reviewer comments --------------------------------------------------------------------

function ReviewRounds({
  paper,
  sourceTitle,
}: {
  paper: Envelope<PaperBody>;
  sourceTitle: (sid: string) => string;
}) {
  const rounds = paper.body.review_rounds;
  return (
    <section aria-labelledby="rounds-title" className="section">
      <h2 id="rounds-title">Reviewer comments</h2>
      {rounds.length === 0 ? (
        <p className="state state-empty">No reviewer comments recorded.</p>
      ) : (
        rounds.map((round) => (
          <div key={round.id} className="review-round" data-testid="review-round">
            <h3>
              {round.label}
              {round.received_at ? (
                <span className="muted"> · received {formatDate(round.received_at)}</span>
              ) : null}
            </h3>
            <ul className="card-list">
              {round.items.map((item) => (
                <li key={item.id} className="card" data-testid="review-item" data-status={item.status}>
                  <div className="card-head">
                    <span className="card-title">{item.text}</span>
                    <span className={`badge badge-${item.status === "resolved" ? "done" : "proposed"}`}>
                      {item.status === "open"
                        ? "Open"
                        : item.status === "drafted"
                          ? "Response drafted (not resolved)"
                          : "Resolved"}
                    </span>
                  </div>
                  <SourceButton
                    spec={{
                      versionId: item.comment_source_ref.version_id,
                      title: sourceTitle(item.comment_source_ref.source_id),
                      startLine: item.comment_source_ref.locator.start_line,
                      endLine: item.comment_source_ref.locator.end_line,
                      citedBy: "Reviewer comment",
                    }}
                  />
                  {item.response_text ? (
                    <p>
                      <span className="muted">Response: </span>
                      {item.response_text}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  );
}
