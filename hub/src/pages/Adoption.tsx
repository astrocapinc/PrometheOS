// Adoption of an existing folder (A05, CP17; decision 0039). The researcher picks an approved
// research root and scans it: PrometheOS inventories every file without writing into the folder
// (the root digest before and after must match), records what it could not read and why, and
// never opens a cloud placeholder. This page shows the scans and their inventory rows. CP20.4
// (decision 0106): the scan's outline (tracks, deliverables, questions, results with their
// producers) is proposed once, corrected here and confirmed once; a family's class proposal from
// two agreeing models is shown as a proposal only.
import { useEffect, useMemo, useState } from "react";
import { describeError, get, post, q } from "../api";
import { Details, ErrorState, PageHeading, QueryView, formatDate } from "../components/ui";
import { ResearchFolders } from "../components/ResearchFolders";
import { SOURCE } from "../components/LayerSection";
import { useHub, useQuery, type QueryState } from "../hub";
import { Link } from "../router";
import type {
  AdoptionAssistTasks,
  AdoptionBaselineAccepted,
  AdoptionBaselines,
  AdoptionClassProposal,
  AdoptionDrift,
  AdoptionEvidenceList,
  AdoptionFamily,
  AdoptionFamilyList,
  AdoptionFiles,
  AdoptionOutline,
  AdoptionPlan,
  AdoptionScan,
  AdoptionScans,
  AiPolicyState,
  AiResults,
  OutlineEdits,
  OutlineItem,
  OutlineKind,
  OutlinePreview,
  RunView,
} from "../types";

const STATUS_TEXT: Record<string, string> = {
  read: "Read",
  listed: "Listed in an archive",
  placeholder: "Cloud placeholder (not opened)",
  excluded: "Excluded",
  noise: "Generated or cache",
  link: "Link (not followed)",
  unreadable: "Could not be read",
  limited: "Stopped at a limit",
};
const PAGE = 100;
const TERMINAL = new Set(["completed", "failed", "cancelled", "unknown_external"]);

function rootAliases(settings: unknown): string[] {
  const roots = (settings as { allowed_roots?: { alias: string }[] } | undefined)?.allowed_roots;
  return (roots ?? []).map((r) => r.alias);
}

function ScanForm({ onStarted }: { onStarted: () => void }) {
  const { project, announce } = useHub();
  const aliases = useMemo(() => rootAliases(project?.settings), [project]);
  const [root, setRoot] = useState("");
  const [exclusions, setExclusions] = useState("");
  const [run, setRun] = useState<RunView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const chosen = root || aliases[0] || "";

  useEffect(() => {
    if (!run || TERMINAL.has(run.state)) return;
    const timer = window.setTimeout(() => {
      get<RunView>(`/runs/${run.run_id}`)
        .then((next) => {
          setRun(next);
          if (TERMINAL.has(next.state)) {
            announce(next.state === "completed" ? "Scan finished." : `Scan ${next.state}.`);
            onStarted();
          }
        })
        .catch((err: unknown) => setError(err));
    }, 800);
    return () => window.clearTimeout(timer);
  }, [run, announce, onStarted]);

  const start = async () => {
    setError(null);
    try {
      const started = await post<RunView>("/adoption/scans", {
        root_alias: chosen,
        exclusions: exclusions
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
        wait_seconds: 2,
      });
      setRun(started);
      announce("Scan started.");
      if (TERMINAL.has(started.state)) onStarted();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    }
  };

  if (aliases.length === 0) {
    return (
      <>
        <p className="muted" data-testid="adoption-no-roots">
          This project has no approved research folder yet. Add the folder that holds your files under
          Research folders below.
        </p>
        <ResearchFolders open />
      </>
    );
  }
  const busy = run !== null && !TERMINAL.has(run.state);
  return (
    <section className="card" aria-labelledby="scan-heading">
      <h2 id="scan-heading">Scan a research folder</h2>
      <p className="muted">
        Reading only: nothing is written into the folder, and cloud placeholders are not
        downloaded.
      </p>
      <div className="form-row">
        <label htmlFor="adoption-root">Folder</label>
        <select id="adoption-root" value={chosen} onChange={(e) => setRoot(e.target.value)} data-testid="adoption-root">
          {aliases.map((alias) => (
            <option key={alias} value={alias}>
              {alias}
            </option>
          ))}
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="adoption-exclusions">Exclude (one path pattern per line, optional)</label>
        <textarea
          id="adoption-exclusions"
          rows={2}
          value={exclusions}
          onChange={(e) => setExclusions(e.target.value)}
          placeholder="**/private/**"
          data-testid="adoption-exclusions"
        />
      </div>
      <div className="button-row">
        <button type="button" className="button primary" disabled={busy} onClick={() => void start()} data-testid="adoption-scan">
          {busy ? "Scanning…" : "Scan"}
        </button>
        {run ? (
          <span className="muted" data-testid="adoption-run-state" data-state={run.state}>
            Run <Link to={`/runs/${run.run_id}`}>{run.run_id.slice(0, 14)}</Link>: {run.state}
            {run.progress ? ` (${run.progress.done}/${run.progress.total ?? "?"} ${run.progress.unit})` : ""}
            {run.progress && run.progress.total && run.state === "running" ? (
              <>
                {" "}
                <progress
                  className="progress"
                  max={run.progress.total}
                  value={Math.min(run.progress.done, run.progress.total)}
                  aria-label="Scan progress"
                />
              </>
            ) : null}
          </span>
        ) : null}
      </div>
      {error ? <ErrorState error={error} /> : null}
    </section>
  );
}

type Unavailable = { analyzer: string; reason: string; install: string; files: number };

// Analyzers this installation could not run for a scan (decision 0053), e.g. MATLAB parsing
// without the adoption-parsers extra. The scan card and the changes view both say so.
function UnavailableNotice({ items }: { items: Unavailable[] | undefined }) {
  return (
    <>
      {(items ?? []).map((u) => (
        <p key={u.analyzer} className="inline-warning" data-testid="adoption-analyzer-unavailable">
          Not analysed by {u.analyzer}: {u.reason}. {u.files} file(s) were inventoried but not parsed, so families
          have less evidence. To add it: {u.install}.
        </p>
      ))}
    </>
  );
}

function Counts({ scan }: { scan: AdoptionScan }) {
  const s = scan.summary;
  if (!s) return <p className="muted">No counts yet.</p>;
  return (
    <p data-testid="adoption-counts">
      {s.files_on_disk} file(s) on disk ({s.bytes_on_disk} bytes), {s.members} archive member(s).{" "}
      {Object.entries(s.by_status)
        .map(([status, n]) => `${n} ${STATUS_TEXT[status]?.toLowerCase() ?? status}`)
        .join("; ")}
      .
    </p>
  );
}

const TIER_TEXT: Record<string, string> = {
  T1: "T1 observed identity or embedded metadata",
  T2: "T2 the producing tool's own record",
  T3: "T3 declared structure",
  T4: "T4 a written statement (quoted)",
  T5: "T5 version-control behaviour",
  T6: "T6 a literal reference",
  T7: "T7 naming or similarity heuristic",
  T8: "T8 unanchored inference (never decisive)",
};

function describeObject(o: AdoptionEvidenceList["evidence"][number]["object"]): string {
  const parts: string[] = [];
  if (o.facet) parts.push(o.facet);
  if (o.path) parts.push(o.path);
  if (o.external) parts.push(o.external);
  if (o.literal !== null && o.literal !== undefined) parts.push(JSON.stringify(o.literal));
  return parts.join(" ");
}

function EvidencePanel({ scan, path, onClose }: { scan: AdoptionScan; path: string; onClose: () => void }) {
  const evidence = useQuery<AdoptionEvidenceList>(
    `/adoption/scans/${scan.scan_gen}/evidence${q({ path, limit: 500 })}`,
  );
  return (
    <section className="card" aria-labelledby="evidence-heading" data-testid="adoption-evidence-panel">
      <div className="card-head">
        <h2 id="evidence-heading">
          What the analyzers found about <code>{path}</code>
        </h2>
        <button type="button" className="button secondary small" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="muted">
        Evidence is graded from T1 (observed) to T8 (inference); a lower tier never overrides a higher one, and
        nothing here is decided until you accept a baseline.
      </p>
      <QueryView query={evidence} loadingLabel="Loading evidence" isEmpty={(d) => d.evidence.length === 0} empty={<>No evidence about this file.</>}>
        {(view) => (
          <ul className="card-list" data-testid="adoption-evidence">
            {view.evidence.map((e) => (
              <li key={e.evidence_id} className="card" data-testid="adoption-evidence-item" data-predicate={e.predicate} data-tier={e.tier}>
                <p>
                  <code>{e.subject}</code> <strong>{e.predicate.replace(/_/g, " ")}</strong> {describeObject(e.object)}
                </p>
                <p className="muted">
                  {TIER_TEXT[e.tier] ?? e.tier}; analyzer {e.analyzer} {e.analyzer_version}
                  {e.locator?.start ? `; line ${e.locator.start}` : ""}
                </p>
                {e.quote ? <blockquote className="quote">{e.quote}</blockquote> : null}
              </li>
            ))}
          </ul>
        )}
      </QueryView>
    </section>
  );
}

function Inventory({ scan }: { scan: AdoptionScan }) {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [focus, setFocus] = useState<string | null>(null);
  useEffect(() => setOffset(0), [status, scan.scan_gen]);
  const files = useQuery<AdoptionFiles>(
    `/adoption/scans/${scan.scan_gen}/files${q({ status: status || undefined, offset, limit: PAGE })}`,
  );
  return (
    <section aria-labelledby="inventory-heading">
      <h2 id="inventory-heading">
        Inventory of scan {scan.scan_gen} ({scan.root_alias})
      </h2>
      <Counts scan={scan} />
      <div className="form-row">
        <label htmlFor="adoption-status">Show</label>
        <select id="adoption-status" value={status} onChange={(e) => setStatus(e.target.value)} data-testid="adoption-status">
          <option value="">Everything</option>
          {Object.entries(STATUS_TEXT).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <QueryView query={files} loadingLabel="Loading the inventory" isEmpty={(d) => d.files.length === 0} empty={<>No rows.</>}>
        {(view) => (
          <>
            <table className="data-table" data-testid="adoption-files">
              <thead>
                <tr>
                  <th scope="col">Path</th>
                  <th scope="col">Status</th>
                  <th scope="col">Type</th>
                  <th scope="col">Zone</th>
                  <th scope="col">Size</th>
                  <th scope="col">Digest</th>
                </tr>
              </thead>
              <tbody>
                {view.files.map((f) => (
                  <tr key={f.path} data-testid="adoption-file" data-status={f.status} data-path={f.path}>
                    <td>
                      <button type="button" className="link-button" onClick={() => setFocus(f.path)} data-testid="adoption-file-open">
                        <code>{f.path}</code>
                      </button>
                      {f.reason ? <div className="muted">{f.reason}</div> : null}
                    </td>
                    <td>{STATUS_TEXT[f.status] ?? f.status}</td>
                    <td>{f.container ?? "-"}</td>
                    <td>{f.zone.replace(/_/g, " ")}</td>
                    <td>{f.size ?? "-"}</td>
                    <td>
                      {f.sha256 ? <code title={f.sha256}>{f.sha256.slice(0, 12)}</code> : "-"}
                      {f.text_sha256 && f.text_sha256 !== f.sha256 ? (
                        <div className="muted" title={f.text_sha256}>
                          text {f.text_sha256.slice(0, 12)}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="button-row">
              <span className="muted">
                Rows {view.offset + 1}-{view.offset + view.files.length} of {view.total}
              </span>
              <button type="button" className="button secondary small" disabled={view.offset === 0} onClick={() => setOffset(Math.max(0, view.offset - PAGE))}>
                Previous
              </button>
              <button
                type="button"
                className="button secondary small"
                disabled={view.next_offset === null}
                onClick={() => setOffset(view.next_offset ?? 0)}
                data-testid="adoption-next"
              >
                Next
              </button>
            </div>
          </>
        )}
      </QueryView>
      {focus ? <EvidencePanel scan={scan} path={focus} onClose={() => setFocus(null)} /> : null}
    </section>
  );
}

// A06.1, CP20.4 (decision 0085): what a family is, in words.
const CLASS_TEXT: Record<string, string> = {
  compete: "Competing files: choose one",
  succession: "Numbered versions",
  variant: "One per context, each current",
  lineage: "A model carried through levels",
  identical: "Identical copies",
  history: "A live file with archived copies",
  run_log: "Run logs",
  tool_output: "Generated output",
};
const DECISION_TEXT: Record<string, string> = {
  variant: "You said: each is current in its own context",
  noise: "You said: none of these matters",
  split: "You said: these are not one family",
};

const PROPOSAL_TEXT: Record<AdoptionClassProposal["answer"], string> = {
  variant: "each is current in its own context",
  succession: "later versions replace earlier ones",
  history: "one live file, the others old copies",
  noise: "none of these matters",
  compete: "they really conflict, so you choose",
};

/** Two agreeing models' proposal for a competing family: shown, never applied (decision 0107). */
function ClassProposal({ proposal }: { proposal: AdoptionClassProposal }) {
  const models = proposal.models.length ? proposal.models.join(" and ") : "your agent";
  return (
    <p className="muted" data-testid="adoption-class-proposal" data-answer={proposal.answer}>
      Suggested by {models}: {PROPOSAL_TEXT[proposal.answer] ?? proposal.answer}
      {proposal.current ? ` (current: ${proposal.current})` : ""}
      {proposal.quote ? ` — “${proposal.quote.text}” (${proposal.quote.path} line ${proposal.quote.line})` : ""}. A
      proposal only; you decide.
    </p>
  );
}

const BAND_TEXT: Record<string, string> = {
  decided: "Decided",
  supported: "Supported",
  weak: "Weak",
  undecided: "Undecided",
};

function FamilyDetail({
  scan,
  familyId,
  onClose,
  onDecided,
}: {
  scan: AdoptionScan;
  familyId: string;
  onClose: () => void;
  onDecided: () => void;
}) {
  const { announce } = useHub();
  const family = useQuery<AdoptionFamily>(`/adoption/scans/${scan.scan_gen}/families/${familyId}`);
  const [error, setError] = useState<unknown>(null);
  const classify = async (decision: "variant" | "noise" | "split" | "revoked") => {
    setError(null);
    try {
      await post("/adoption/classify", { scan_gen: scan.scan_gen, family_id: familyId, decision });
      announce("Recorded.");
      family.reload();
      onDecided();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    }
  };
  const decide = async (path: string) => {
    setError(null);
    try {
      await post<AdoptionFamily>("/adoption/decide", { scan_gen: scan.scan_gen, family_id: familyId, current: path });
      announce("Current file chosen.");
      family.reload();
      onDecided();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    }
  };
  return (
    <section className="card" aria-labelledby="family-heading" data-testid="adoption-family-detail">
      <QueryView query={family} loadingLabel="Loading the family">
        {(f) => (
          <>
            <div className="card-head">
              <h3 id="family-heading">
                {f.key} <span className={`badge badge-${f.band}`}>{BAND_TEXT[f.band] ?? f.band}</span>
              </h3>
              <button type="button" className="button secondary small" onClick={onClose}>
                Close
              </button>
            </div>
            <p data-testid="adoption-family-class" data-class={f.class}>
              <strong>{CLASS_TEXT[f.class] ?? f.class}</strong>
              <span className="muted"> · {f.class_reason}</span>
              {f.class_decision ? <span> · {DECISION_TEXT[f.class_decision]}</span> : null}
              {f.class_decision ? (
                <>
                  {" "}
                  <button
                    type="button"
                    className="button secondary small"
                    onClick={() => void classify("revoked")}
                    data-testid="adoption-classify-undo"
                  >
                    Undo my decision
                  </button>
                </>
              ) : null}
            </p>
            <p className="muted">
              {f.current ? (
                <>
                  {f.deciding_rule === "R1" ? "You chose" : "Proposed current member:"} <code>{f.current}</code> (rule{" "}
                  {f.deciding_rule}). Nothing is final until you accept a baseline.
                </>
              ) : (
                "No member could be singled out: this family needs your decision."
              )}
            </p>
            {f.class_proposal ? <ClassProposal proposal={f.class_proposal} /> : null}
            {f.needs_owner ? (
              <div className="button-row" data-testid="adoption-classify">
                <span className="muted">Not competition?</span>
                <button type="button" className="button secondary small" onClick={() => void classify("variant")}>
                  Each is current in its own context
                </button>
                <button type="button" className="button secondary small" onClick={() => void classify("noise")}>
                  None of these matters
                </button>
                <button type="button" className="button secondary small" onClick={() => void classify("split")}>
                  Not one family
                </button>
              </div>
            ) : null}
            <table className="data-table" data-testid="adoption-family-members">
              <thead>
                <tr>
                  <th scope="col">Rank</th>
                  <th scope="col">File</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Why</th>
                  <th scope="col">Your choice</th>
                </tr>
              </thead>
              <tbody>
                {f.members.map((m) => (
                  <tr key={m.path} data-testid="adoption-family-member" data-outcome={m.outcome} data-path={m.path}>
                    <td>{m.rank ?? "-"}</td>
                    <td>
                      <code>{m.path}</code>
                      {m.copy_of ? <div className="muted">same content as {m.copy_of}</div> : null}
                    </td>
                    <td>{m.outcome}</td>
                    <td>
                      {m.reasons.length === 0 ? <span className="muted">-</span> : null}
                      <ul className="plain-list">
                        {m.reasons.map((r, i) => (
                          <li key={i}>
                            <strong>{r.rule}</strong> ({r.tier}) {r.text}
                          </li>
                        ))}
                      </ul>
                    </td>
                    <td>
                      {m.outcome === "copy" ? null : f.current === m.path && f.deciding_rule === "R1" ? (
                        <span className="badge badge-decided">chosen</span>
                      ) : (
                        <button
                          type="button"
                          className="button secondary small"
                          onClick={() => void decide(m.path)}
                          data-testid="adoption-decide"
                        >
                          Make current
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {f.conflicts.length ? (
              <div data-testid="adoption-family-conflicts">
                <h4>Kept contradictions</h4>
                <ul>
                  {f.conflicts.map((c, i) => (
                    <li key={i} data-kind={c.kind}>
                      {c.text}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {error ? <ErrorState error={error} /> : null}
          </>
        )}
      </QueryView>
    </section>
  );
}

const OUTLINE_KINDS: [OutlineKind, string][] = [
  ["track", "Tracks"],
  ["deliverable", "Deliverables"],
  ["question", "Questions"],
  ["result", "Results and the scripts that wrote them"],
  ["stale_candidate", "Results older than their script"],
];
const EDITABLE = new Set<OutlineKind>(["track", "deliverable", "question"]);

function sourceText(item: OutlineItem): string {
  const models = item.models.length ? item.models.join(", ") : "your agent";
  const proposed = `proposed by ${models} — a hint you confirm`;
  if (item.source === "rule") return "from the folder";
  if (item.source === "proposed") return proposed;
  return `from the folder; also ${proposed}`;
}

function Cites({ item }: { item: OutlineItem }) {
  return (
    <ul className="plain-list outline-cites">
      {item.cites.map((c, i) => (
        <li key={`${c.path}-${c.line ?? ""}-${i}`} data-testid="outline-cite">
          {item.kind === "track" ? (
            <>
              folder <code>{c.path}</code>
            </>
          ) : (
            <>
              {c.quote ? <q>{c.quote}</q> : null}{" "}
              <span className="muted">
                {c.path}
                {c.line ? ` line ${c.line}` : ""}
              </span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

/** The owner's corrections as the confirmation's edits: only what differs from the proposal. A
 * question whose deliverable or tracks were left out moves with them (shown in its controls). */
function editsOf(
  outline: AdoptionOutline,
  drop: Set<string>,
  titles: Record<string, string>,
  places: Record<string, string | null>,
  tracks: Record<string, string[]>,
): OutlineEdits {
  const edits: OutlineEdits = { drop: [...drop].sort(), titles: {}, addresses: {}, draws_on: {} };
  for (const item of outline.items) {
    if (drop.has(item.item_id)) continue;
    const title = titles[item.item_id]?.trim();
    if (title && title !== item.title) edits.titles[item.item_id] = title;
    if (item.kind !== "question") continue;
    const place = placeOf(item, drop, places);
    if (place !== (item.addresses ?? null)) edits.addresses[item.item_id] = place;
    const chosen = tracksOf(item, drop, tracks);
    if (!sameList(chosen, item.draws_on ?? [])) edits.draws_on[item.item_id] = chosen;
  }
  return edits;
}

function placeOf(item: OutlineItem, drop: Set<string>, places: Record<string, string | null>): string | null {
  const place = (item.item_id in places ? places[item.item_id] : item.addresses) ?? null;
  return place && place !== "objective" && drop.has(place) ? null : place;
}

function tracksOf(item: OutlineItem, drop: Set<string>, tracks: Record<string, string[]>): string[] {
  return (tracks[item.item_id] ?? item.draws_on ?? []).filter((t) => !drop.has(t)).sort();
}

function OutlinePanel({ scan }: { scan: AdoptionScan }) {
  const { announce } = useHub();
  const outline = useQuery<AdoptionOutline>(`/adoption/scans/${scan.scan_gen}/outline`);
  const [drop, setDrop] = useState<Set<string>>(new Set());
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [places, setPlaces] = useState<Record<string, string | null>>({});
  const [tracks, setTracks] = useState<Record<string, string[]>>({});
  const [preview, setPreview] = useState<{ key: string; counts: OutlinePreview } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setDrop(new Set());
    setTitles({});
    setPlaces({});
    setTracks({});
    setPreview(null);
  }, [scan.scan_gen]);
  const data = outline.data;
  const edits = useMemo(
    () => (data ? editsOf(data, drop, titles, places, tracks) : null),
    [data, drop, titles, places, tracks],
  );
  const key = edits ? JSON.stringify(edits) : "";
  useEffect(() => {
    if (!data || !edits) return;
    let live = true;
    const timer = window.setTimeout(() => {
      post<OutlinePreview>("/adoption/outline/preview", {
        scan_gen: data.scan_gen,
        proposal_sha256: data.sha256,
        edits,
      })
        .then((counts) => {
          if (live) {
            setPreview({ key, counts });
            setError(null);
          }
        })
        .catch((err: unknown) => {
          if (live) setError(err);
        });
    }, 250);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [data, edits, key]);
  const confirm = async () => {
    if (!data || !edits) return;
    setBusy(true);
    setError(null);
    try {
      await post("/adoption/outline/confirm", { scan_gen: data.scan_gen, proposal_sha256: data.sha256, edits });
      announce("Outline confirmed.");
      outline.reload();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const toggle = (id: string) => {
    setDrop((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  return (
    <section aria-labelledby="outline-heading" data-testid="adoption-outline-section">
      <h2 id="outline-heading">Outline</h2>
      <p className="muted">
        What this folder looks like as research: the tracks it forks by, the papers in it, the questions its notes ask,
        and which script wrote which result. Leave out what is wrong, correct the titles and say where each question
        belongs, then confirm once. Nothing is written into the folder.
      </p>
      <QueryView query={outline} loadingLabel="Loading the outline">
        {(o) => {
          const confirmedHere = o.confirmed && o.confirmed.proposal_sha256 === o.sha256 ? o.confirmed : null;
          const deliverables = o.items.filter((i) => i.kind === "deliverable" && !drop.has(i.item_id));
          const keptTracks = o.items.filter((i) => i.kind === "track" && !drop.has(i.item_id));
          const titleOf = (i: OutlineItem) => titles[i.item_id] ?? i.title;
          return (
            <>
              {confirmedHere ? (
                <p className="info" data-testid="outline-confirmed" data-outline-id={confirmedHere.outline_id}>
                  Confirmed {formatDate(confirmedHere.confirmed_at)}.
                </p>
              ) : o.confirmed ? (
                <p className="muted" data-testid="outline-confirmed-earlier">
                  You confirmed an earlier outline of this folder on {formatDate(o.confirmed.confirmed_at)} (scan{" "}
                  {o.confirmed.scan_gen}); this one is new to you.
                </p>
              ) : null}
              {o.items.length === 0 ? <p className="muted">PrometheOS found nothing to outline in this scan.</p> : null}
              {OUTLINE_KINDS.map(([kind, heading]) => {
                const items = o.items.filter((i) => i.kind === kind);
                if (!items.length) return null;
                const editable = EDITABLE.has(kind) && !confirmedHere;
                return (
                  <div key={kind} className="outline-group" data-testid="outline-group" data-kind={kind}>
                    <h3>
                      {heading} ({items.length})
                    </h3>
                    <ul className="card-list">
                      {items.map((item) => {
                        const kept = !drop.has(item.item_id);
                        const place = placeOf(item, drop, places);
                        const chosen = tracksOf(item, drop, tracks);
                        return (
                          <li
                            key={item.item_id}
                            className="card outline-item"
                            data-testid="outline-item"
                            data-kind={item.kind}
                            data-item-id={item.item_id}
                            data-kept={kept ? "yes" : "no"}
                          >
                            {editable ? (
                              <div className="form-row">
                                <label>
                                  <input
                                    type="checkbox"
                                    checked={kept}
                                    onChange={() => toggle(item.item_id)}
                                    data-testid="outline-keep"
                                  />{" "}
                                  Keep
                                </label>
                                <input
                                  aria-label={`Title of ${item.title}`}
                                  value={titleOf(item)}
                                  maxLength={200}
                                  disabled={!kept}
                                  onChange={(e) => setTitles((t) => ({ ...t, [item.item_id]: e.target.value }))}
                                  data-testid="outline-title"
                                />
                              </div>
                            ) : (
                              <p className="card-title">{item.title}</p>
                            )}
                            {item.kind === "result" || item.kind === "stale_candidate" ? (
                              <p data-testid="outline-result">
                                <code>{item.path}</code>
                                {item.kind === "stale_candidate" ? " is older than its script " : " written by "}
                                <code>{item.producer}</code>
                              </p>
                            ) : null}
                            {item.kind === "track" && item.files ? (
                              <p className="muted">{item.files} file(s) laid out by this folder</p>
                            ) : null}
                            <Cites item={item} />
                            <p className="muted" data-testid="outline-source" data-source={item.source}>
                              {sourceText(item)}
                            </p>
                            {item.kind === "question" && editable && kept ? (
                              <div className="outline-place">
                                <div className="form-row">
                                  <label htmlFor={`place-${item.item_id}`}>Belongs to</label>
                                  <select
                                    id={`place-${item.item_id}`}
                                    value={place ?? ""}
                                    onChange={(e) =>
                                      setPlaces((p) => ({ ...p, [item.item_id]: e.target.value || null }))
                                    }
                                    data-testid="outline-place"
                                  >
                                    {deliverables.map((d) => (
                                      <option key={d.item_id} value={d.item_id}>
                                        {titleOf(d)}
                                      </option>
                                    ))}
                                    <option value="objective">The project objective</option>
                                    <option value="">Nowhere yet</option>
                                  </select>
                                </div>
                                {keptTracks.length ? (
                                  <fieldset className="outline-tracks">
                                    <legend>Draws on</legend>
                                    {keptTracks.map((t) => (
                                      <label key={t.item_id}>
                                        <input
                                          type="checkbox"
                                          checked={chosen.includes(t.item_id)}
                                          onChange={() =>
                                            setTracks((all) => {
                                              const now = tracksOf(item, drop, all);
                                              const next = now.includes(t.item_id)
                                                ? now.filter((x) => x !== t.item_id)
                                                : [...now, t.item_id];
                                              return { ...all, [item.item_id]: next.sort() };
                                            })
                                          }
                                          data-testid="outline-track"
                                          data-track-id={t.item_id}
                                        />{" "}
                                        {titleOf(t)}
                                      </label>
                                    ))}
                                  </fieldset>
                                ) : null}
                              </div>
                            ) : null}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                );
              })}
              {o.truncated ? <p className="muted">Only the first items are listed.</p> : null}
              {!confirmedHere && o.items.length ? (
                <div className="stage-confirm" data-testid="outline-preview">
                  {preview && preview.key === key ? (
                    <p data-testid="outline-counts">
                      Confirming records {preview.counts.kept.track} track(s), {preview.counts.kept.question} question(s)
                      and {preview.counts.kept.deliverable} paper(s): {preview.counts.records_created} new,{" "}
                      {preview.counts.records_reused} already recorded, and {preview.counts.relations} link(s);{" "}
                      {preview.counts.dropped} left out
                      {preview.counts.unplaced_questions
                        ? `; ${preview.counts.unplaced_questions} question(s) placed nowhere`
                        : ""}
                      .
                      {preview.counts.objective_set ? null : (
                        <> The project has no objective entry, so questions for the objective are kept but not linked.</>
                      )}
                    </p>
                  ) : (
                    <p className="muted">Working out what confirming records…</p>
                  )}
                  <button
                    type="button"
                    className="button primary"
                    disabled={busy || !preview || preview.key !== key}
                    onClick={() => void confirm()}
                    data-testid="outline-confirm"
                  >
                    Confirm outline
                  </button>
                </div>
              ) : null}
              {error ? <ErrorState error={error} /> : null}
            </>
          );
        }}
      </QueryView>
    </section>
  );
}

function Families({ scan, onChanged }: { scan: AdoptionScan; onChanged: () => void }) {
  const list = useQuery<AdoptionFamilyList>(`/adoption/scans/${scan.scan_gen}/families${q({ limit: 500 })}`);
  const [focus, setFocus] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  useEffect(() => setFocus(null), [scan.scan_gen]);
  return (
    <section aria-labelledby="families-heading" data-testid="adoption-families-section">
      <h2 id="families-heading">Families and proposed current members</h2>
      <p className="muted">
        Files that are versions or copies of one thing, with the member PrometheOS proposes as current and why. A
        filesystem date never decides; names like FINAL or BEST rank nothing up.
      </p>
      <QueryView
        query={list}
        loadingLabel="Loading families"
        isEmpty={(d) => d.families.length === 0}
        empty={<>No families: no two files here look like versions or copies of one thing.</>}
      >
        {(view) => {
          const counts = new Map<string, number>();
          for (const f of view.families) counts.set(f.class, (counts.get(f.class) ?? 0) + 1);
          const asking = view.families.filter((f) => f.needs_owner);
          const shown = all ? view.families : asking;
          return (
          <>
          <p data-testid="adoption-class-summary">
            {view.total} families; <strong>{asking.length} need you</strong>
            {view.total > view.families.length ? ` (of the first ${view.families.length})` : ""}.{" "}
            <span className="muted">
              {[...counts.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([c, n]) => `${n} ${(CLASS_TEXT[c] ?? c).toLowerCase()}`)
                .join(" · ")}
            </span>
          </p>
          <button
            type="button"
            className="button secondary small"
            onClick={() => setAll(!all)}
            data-testid="adoption-show-all"
          >
            {all ? "Show only the families that need you" : `Show all ${view.families.length} families`}
          </button>
          <table className="data-table" data-testid="adoption-families">
            <thead>
              <tr>
                <th scope="col">Family</th>
                <th scope="col">Class</th>
                <th scope="col">Proposed current</th>
                <th scope="col">Members</th>
                <th scope="col">Conflicts</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((f) => (
                <tr
                  key={f.family_id}
                  data-testid="adoption-family"
                  data-band={f.band}
                  data-class={f.class}
                  data-needs-owner={f.needs_owner ? "yes" : "no"}
                  data-family-id={f.family_id}
                >
                  <td>
                    <button type="button" className="link-button" onClick={() => setFocus(f.family_id)} data-testid="adoption-family-open">
                      {f.key}
                    </button>
                  </td>
                  <td>
                    {CLASS_TEXT[f.class] ?? f.class}
                    {f.class_decision ? <span className="muted"> ({f.class_decision})</span> : null}
                  </td>
                  <td>
                    {f.needs_owner ? (
                      <span className="muted">needs your decision</span>
                    ) : f.current ? (
                      <code>{f.current}</code>
                    ) : (
                      <span className="muted">no choice needed</span>
                    )}
                  </td>
                  <td>{f.members}</td>
                  <td>{f.conflicts}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </>
          );
        }}
      </QueryView>
      {focus ? (
        <FamilyDetail scan={scan} familyId={focus} onClose={() => setFocus(null)} onDecided={() => { list.reload(); onChanged(); }} />
      ) : null}
    </section>
  );
}

// The models the standing policy grants each task, in the policy's order (task kind -> models).
function grantedModels(state: AiPolicyState | undefined): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const doc = state?.in_force ? state.policy?.policy : undefined;
  const grants = (doc?.tasks ?? []) as { task_kind: string; models: { model: string }[] }[];
  for (const g of grants) out.set(g.task_kind, g.models.map((m) => m.model));
  return out;
}

// The layer's answer to one reading, as a run would give it: the task's current version, and for
// each sample index the policy's first model with a sample (newest first otherwise); when a task's
// samples say different things there is no proposal (decision 0104).
function layerAnswer(
  rows: AiResults["results"],
  kind: string,
  subject: string,
  granted: Map<string, string[]>,
): string | null {
  const all = rows.filter((r) => r.task_kind === `adoption.${kind}` && r.subject === subject);
  if (all.length === 0) return null;
  const version = Math.max(...all.map((r) => r.task_version));
  const order = granted.get(`adoption.${kind}`) ?? [];
  const rank = (model: string) => (order.includes(model) ? order.indexOf(model) : order.length);
  const mine = all
    .filter((r) => r.task_version === version)
    .map((r, i) => ({ r, i }))
    .sort((a, b) => rank(a.r.model) - rank(b.r.model) || a.i - b.i)
    .map((x) => x.r);
  const newest = new Map<number, AiResults["results"][number]>();
  const used = new Set<string>();
  for (const r of mine) {
    // a k-sample run takes each sample from a model not used yet (R4-F2)
    if (!newest.has(r.sample) && !used.has(r.model)) {
      newest.set(r.sample, r);
      used.add(r.model);
    }
  }
  const samples = [...newest.values()];
  // samples agree on the full claim (lines and dates too), as the run decides (review cp22-a H5)
  const said = new Set(samples.map((r) => r.claim ?? r.outcome));
  if (samples.length > 1 && said.size > 1) return "the models disagree, so there is no proposal";
  const first = newest.get(0) ?? samples[0];
  if (!first) return null;
  if (first.outcome === "rejected") return `rejected by the rules (${first.reasons.join(", ")})`;
  return `${first.summary ?? first.outcome}${first.hint_only ? " (a hint)" : ""}`;
}

function AgentTasks({ scan }: { scan: AdoptionScan }) {
  const tasks = useQuery<AdoptionAssistTasks>(`/adoption/scans/${scan.scan_gen}/assist`);
  // This scan's answers of the layer itself, chosen by the service (R5-F2, decision 0104).
  const results = useQuery<AiResults>(`/ai/results${q({ scan_gen: scan.scan_gen, current: "true", limit: 1000 })}`);
  const rows = results.data?.results ?? [];
  const policy = useQuery<AiPolicyState>("/ai/policy");
  const granted = grantedModels(policy.data);
  return (
    <section aria-labelledby="assist-heading" data-testid="adoption-assist-section">
      <h2 id="assist-heading">Reading your agent can do</h2>
      <p className="muted">
        Tasks your coding agent can do with its own tools when you ask it: read status statements in notes, run
        MATLAB&apos;s dependency analysis, name a file&apos;s role. PrometheOS checks every quote against the exact
        file and needs agreeing answers before using one; an agent&apos;s reading never overrides what the files
        themselves show.
      </p>
      <QueryView query={tasks} loadingLabel="Loading tasks" isEmpty={(d) => d.tasks.length === 0} empty={<>Nothing to ask.</>}>
        {(view) => (
          <ul className="plain-list" data-testid="adoption-assist-tasks">
            {view.tasks.map((t) => (
              <li key={`${t.task_kind}:${t.subject}`} data-testid="adoption-assist-task" data-done={t.done ? "yes" : "no"}>
                <code>{t.subject}</code>: {t.task_kind.replace(/_/g, " ")} ({t.reason}){t.done ? ", done" : ""}
                {layerAnswer(rows, t.task_kind, t.subject, granted) ? (
                  <span className="muted" data-testid="adoption-assist-layer">
                    {" "}
                    · the layer: {layerAnswer(rows, t.task_kind, t.subject, granted)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </QueryView>
      <LayerLine rows={rows} policy={policy} />
    </section>
  );
}

// CP22.4 (S71): where the intelligence layer stands for these readings, what runs instead when
// it is off, and its proposals for this scan, each tagged with its source. A proposal is a hint
// until the owner uses it as evidence on the AI page; none decides a family or closes an item.
function LayerLine({ rows, policy }: { rows: AiResults["results"]; policy: QueryState<AiPolicyState> }) {
  // One reading per file and task (the first sample), folded away when there are many.
  const seen = new Set<string>();
  const mine = rows.filter((r) => {
    const key = `${r.task_kind}:${r.subject}`;
    if (r.sample !== 0 || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const list = (
    <ul className="plain-list" data-testid="adoption-ai-proposals">
      {mine.map((r) => (
        <li key={r.result_key} data-testid="adoption-ai-proposal" data-source={r.executor} data-outcome={r.outcome}>
          <code>{r.subject}</code>: {r.task_kind.replace(/^adoption\./, "").replace(/_/g, " ")}, {r.outcome}
          {r.summary ? ` (${r.summary})` : ""}
          {r.hint_only ? " (a hint)" : ""} · {SOURCE[r.executor] ?? r.executor}
        </li>
      ))}
    </ul>
  );
  return (
    <div data-testid="adoption-ai" className="small-print">
      <QueryView query={policy} loadingLabel="Loading the intelligence layer">
        {(p) =>
          p.in_force ? (
            <p data-testid="adoption-ai-fallback" data-layer="on">
              The intelligence layer may propose these readings for files under a normal-sensitivity root, within your
              standing policy. Its proposals are hints, checked by the same rules, until you use one as evidence on the{" "}
              <Link to="/ai">AI page</Link>.
            </p>
          ) : (
            <p data-testid="adoption-ai-fallback" data-layer="off">
              The intelligence layer is off, so nothing here is sent anywhere: the rules&apos; own results stand, and your
              coding agent can do these readings when you ask it.
            </p>
          )
        }
      </QueryView>
      {mine.length > 8 ? (
        <Details label={`The layer's readings of ${mine.length} files`}>{list}</Details>
      ) : mine.length ? (
        list
      ) : null}
    </div>
  );
}

const STATE_TEXT: Record<string, string> = {
  ready: "To do",
  reported: "Reported",
  validated: "Result checked",
  rejected: "Result rejected",
  accepted: "Accepted",
  settled: "Decided",
  superseded: "No longer needed",
};

function PlanPanel({ scan, version, adopted }: { scan: AdoptionScan; version: number; adopted: string[] }) {
  const { announce } = useHub();
  const plan = useQuery<AdoptionPlan>(`/adoption/scans/${scan.scan_gen}/plan`);
  const reloadPlan = plan.reload;
  useEffect(() => {
    if (version > 0) reloadPlan();
  }, [version, reloadPlan]);
  const [error, setError] = useState<unknown>(null);
  const [baseline, setBaseline] = useState<AdoptionBaselineAccepted | null>(null);
  const act = async (path: string, body: Record<string, unknown>, done: string) => {
    setError(null);
    try {
      const result = await post<AdoptionBaselineAccepted | Record<string, unknown>>(path, body);
      if (path === "/adoption/baselines") setBaseline(result as AdoptionBaselineAccepted);
      announce(done);
      plan.reload();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    }
  };
  return (
    <section aria-labelledby="plan-heading" data-testid="adoption-plan-section">
      <h2 id="plan-heading">Consolidation plan</h2>
      <p className="muted">
        What is left to settle, as work items: your decisions, and checks your agent can do and report (it never writes,
        moves or deletes anything in the folder). Accepting a baseline fixes the current files and the checked results.
      </p>
      <QueryView query={plan} loadingLabel="Loading the plan">
        {(view) => (
          <>
            {view.version === 0 ? (
              <p className="muted">No plan yet.</p>
            ) : (
              <ul className="card-list" data-testid="adoption-plan-steps">
                {view.steps
                  .filter((s) => s.state !== "superseded")
                  .map((s) => (
                    <li
                      key={s.step.step_id}
                      className="card"
                      data-testid="adoption-plan-step"
                      data-kind={s.step.kind}
                      data-state={s.state}
                    >
                      <div className="card-head">
                        <p className="card-title">{s.step.title}</p>
                        <span className={`badge badge-${s.state}`}>{STATE_TEXT[s.state] ?? s.state}</span>
                      </div>
                      <p className="muted">
                        {s.step.actor === "owner" ? "Yours to decide" : "Your agent can do this"}: {s.step.why}
                      </p>
                      {s.result?.problems?.length ? (
                        <ul className="plain-list">
                          {s.result.problems.map((p, i) => (
                            <li key={i}>{p}</li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  ))}
              </ul>
            )}
            <div className="button-row">
              <button
                type="button"
                className="button secondary"
                onClick={() => void act("/adoption/plan", { scan_gen: scan.scan_gen }, "Plan updated.")}
                data-testid="adoption-plan-create"
              >
                {view.version === 0 ? "Make a plan" : "Update the plan"}
              </button>
              <button
                type="button"
                className="button primary"
                onClick={() => void act("/adoption/baselines", { scan_gen: scan.scan_gen }, "Baseline accepted.")}
                data-testid="adoption-baseline-accept"
              >
                Accept as baseline
              </button>
            </div>
          </>
        )}
      </QueryView>
      {baseline ? <ExportForm baselineId={baseline.baseline_id} adopted={adopted} /> : null}
      {baseline ? (
        <p data-testid="adoption-baseline" data-baseline-id={baseline.baseline_id}>
          Baseline <code>{baseline.baseline_id}</code> accepted ({baseline.accepted_steps} checked result(s) accepted;
          plan version {baseline.plan.version}).
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </section>
  );
}

const CHANGE_TEXT: Record<string, string> = {
  added: "Added",
  changed: "Changed",
  moved: "Moved",
  deleted: "Deleted",
  kept: "Kept",
  undecided: "Undecided",
};

function DriftList({ drift, label }: { drift: AdoptionDrift; label: string }) {
  const c = drift.counts;
  return (
    <div className="card" data-testid="adoption-drift" data-against={drift.against.kind}>
      <p className="card-title">{label}</p>
      <p data-testid="adoption-drift-counts">
        {c.added} added, {c.changed} changed, {c.moved} moved, {c.deleted} deleted. Analysed again:{" "}
        {drift.analysis.extracted} file(s); reused: {drift.analysis.replayed}.
      </p>
      <UnavailableNotice items={drift.analysis.analyzers_unavailable} />
      {drift.entries.length ? (
        <ul className="plain-list">
          {drift.entries.slice(0, 50).map((e) => (
            <li key={`${e.change}:${e.path}`} data-testid="adoption-drift-entry" data-change={e.change}>
              {CHANGE_TEXT[e.change]}: <code>{e.path}</code>
              {e.from ? (
                <>
                  {" "}
                  (from <code>{e.from}</code>)
                </>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {drift.families.length ? (
        <ul className="plain-list" data-testid="adoption-drift-families">
          {drift.families
            .filter((f) => f.change !== "kept")
            .map((f) => (
              <li key={f.family_id} data-testid="adoption-drift-family" data-change={f.change}>
                {f.key}: the accepted current file was {CHANGE_TEXT[f.change]?.toLowerCase() ?? f.change}
                {f.now && f.now !== f.current ? (
                  <>
                    {" "}
                    (now <code>{f.now}</code>)
                  </>
                ) : null}
              </li>
            ))}
        </ul>
      ) : null}
    </div>
  );
}

function ChangesPanel({ scan }: { scan: AdoptionScan }) {
  const baselines = useQuery<AdoptionBaselines>(`/adoption/baselines${q({ root_alias: scan.root_alias })}`);
  const reloadBaselines = baselines.reload;
  useEffect(() => reloadBaselines(), [scan.scan_gen, reloadBaselines]); // a baseline may be newer than the list
  const latest = baselines.data?.baselines.find((b) => b.scan_gen !== scan.scan_gen) ?? null;
  const previous = useQuery<AdoptionDrift>(
    scan.config.previous_gen ? `/adoption/scans/${scan.scan_gen}/drift` : null,
  );
  const sinceBaseline = useQuery<AdoptionDrift>(
    latest ? `/adoption/scans/${scan.scan_gen}/drift${q({ against: latest.baseline_id })}` : null,
  );
  if (!scan.config.previous_gen && !latest) return null;
  return (
    <section aria-labelledby="changes-heading" data-testid="adoption-changes-section">
      <h2 id="changes-heading">Changes</h2>
      <p className="muted">
        Files are compared by content, never by date: a file whose content now sits at another path was moved.
      </p>
      {previous.data ? <DriftList drift={previous.data} label={`Since scan ${scan.config.previous_gen}`} /> : null}
      {sinceBaseline.data ? (
        <DriftList drift={sinceBaseline.data} label={`Since the baseline accepted ${formatDate(latest?.accepted_at ?? "")}`} />
      ) : null}
      {previous.error ? <ErrorState error={previous.error} /> : null}
    </section>
  );
}

function ExportForm({ baselineId, adopted }: { baselineId: string; adopted: string[] }) {
  const { project, announce } = useHub();
  // never into a folder PrometheOS adopts (review cp17-6-a F2)
  const aliases = useMemo(
    () => rootAliases(project?.settings).filter((alias) => !adopted.includes(alias)),
    [project, adopted],
  );
  const [root, setRoot] = useState("");
  const [folder, setFolder] = useState("PrometheOS");
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const chosen = root || aliases[0] || "";
  if (aliases.length === 0) {
    return (
      <p className="muted" data-testid="adoption-export-none">
        To write the baseline into your notes, approve a notes folder (for example your vault) in the project
        settings; PrometheOS never writes into a folder it adopts.
      </p>
    );
  }
  const write = async () => {
    setError(null);
    try {
      const result = await post<{ note: string }>("/adoption/export/markdown", {
        baseline_id: baselineId,
        root_alias: chosen,
        folder,
      });
      setDone(result.note);
      announce("Note written.");
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    }
  };
  return (
    <div className="card" data-testid="adoption-export">
      <p className="card-title">Write the baseline to your notes</p>
      <p className="muted">
        PrometheOS writes its own block into <code>adoption-…md</code>; anything you write outside the block is kept.
      </p>
      <div className="form-row">
        <label htmlFor="adoption-export-root">Folder</label>
        <select id="adoption-export-root" value={chosen} onChange={(e) => setRoot(e.target.value)}>
          {aliases.map((alias) => (
            <option key={alias} value={alias}>
              {alias}
            </option>
          ))}
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="adoption-export-folder">Inside it</label>
        <input id="adoption-export-folder" value={folder} onChange={(e) => setFolder(e.target.value)} />
      </div>
      <button type="button" className="button secondary" onClick={() => void write()} data-testid="adoption-export-write">
        Write note
      </button>
      {done ? (
        <p data-testid="adoption-export-done">
          Written: <code>{done}</code>
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
    </div>
  );
}

export function AdoptionPage() {
  const scans = useQuery<AdoptionScans>("/adoption/scans");
  const [chosen, setChosen] = useState<number | null>(null);
  const [changes, setChanges] = useState(0);
  const list = scans.data?.scans ?? [];
  const adoptedAliases = useMemo(() => [...new Set(list.map((s) => s.root_alias))], [list]);
  const selected = list.find((s) => s.scan_gen === chosen) ?? list[0] ?? null;
  const { project } = useHub();
  const hasRoots = rootAliases(project?.settings).length > 0;
  return (
    <>
      <PageHeading
        title="Adoption"
        lead="Map an existing working folder into the record without changing it: every file, what it is, and what could not be read."
      />
      {hasRoots ? <ResearchFolders /> : null}
      <ScanForm onStarted={scans.reload} />
      <QueryView query={scans} loadingLabel="Loading scans" isEmpty={(d) => d.scans.length === 0} empty={<>No folder has been scanned yet.</>}>
        {(view) => (
          <section aria-labelledby="scans-heading">
            <h2 id="scans-heading">Scans</h2>
            <ul className="card-list" data-testid="adoption-scans">
              {view.scans.map((s) => (
                <li key={s.scan_gen} className="card" data-testid="adoption-scan-card" data-state={s.state} data-scan-gen={s.scan_gen}>
                  <div className="card-head">
                    <p className="card-title">
                      Scan {s.scan_gen} of {s.root_alias}
                    </p>
                    <span className={`badge badge-${s.state}`}>{s.state}</span>
                  </div>
                  <p className="muted">
                    Started {formatDate(s.started_at)}
                    {s.finished_at ? `, finished ${formatDate(s.finished_at)}` : ""}.{" "}
                    {s.state === "complete" ? "The folder was unchanged by the scan (root digest equal before and after)." : null}
                    {s.error ? ` ${s.error}` : null}
                  </p>
                  <UnavailableNotice items={s.analysis?.analyzers_unavailable} />
                  <button type="button" className="button secondary small" onClick={() => setChosen(s.scan_gen)} data-testid="adoption-open-scan">
                    Show inventory
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </QueryView>
      {selected && selected.state === "complete" ? <OutlinePanel scan={selected} /> : null}
      {selected && selected.state === "complete" ? (
        <Families scan={selected} onChanged={() => setChanges((n) => n + 1)} />
      ) : null}
      {selected && selected.state === "complete" ? <PlanPanel scan={selected} version={changes} adopted={adoptedAliases} /> : null}
      {selected && selected.state === "complete" ? <ChangesPanel scan={selected} /> : null}
      {selected && selected.state === "complete" ? <AgentTasks scan={selected} /> : null}
      {selected ? <Inventory scan={selected} /> : null}
      {scans.error ? <ErrorState error={scans.error} /> : null}
      <p>
        <Link to="/now">Back to Now</Link>
      </p>
    </>
  );
}
