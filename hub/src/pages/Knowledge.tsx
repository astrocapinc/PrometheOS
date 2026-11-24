// Knowledge: search with kind filters, scoped claims with scope and state, corrections
// (proposed vs applied) and backlinks on demand.
import { useMemo, useState, type FormEvent } from "react";
import { get, q } from "../api";
import {
  Details,
  Empty,
  ErrorState,
  KeyValues,
  Loading,
  PageHeading,
  QueryView,
  SourceButton,
  StateBadge,
  humanizeIds,
  kindLabel,
  kindOf,
} from "../components/ui";
import { recordPath, recordTitle, useKindList, useRecords } from "../data";
import { useHandoff } from "../handoff";
import { useHub, useQuery } from "../hub";
import { Link, useHashTarget } from "../router";
import type {
  AssumptionRef,
  ClaimBody,
  CorrectionBody,
  EntryBody,
  Envelope,
  Links,
  NowView,
  SearchHit,
  SourceBody,
} from "../types";

const FILTERS = [
  ["all", "All kinds"],
  ["claim", "Claims"],
  ["entry", "Notebook entries"],
  ["item", "Meeting items"],
  ["version", "Source text"],
  ["correction", "Corrections"],
] as const;

export function KnowledgePage() {
  const claims = useKindList<ClaimBody>("claim");
  const corrections = useKindList<CorrectionBody>("correction");
  const entries = useKindList<EntryBody>("entry");
  const now = useQuery<NowView>("/now?limit=200");
  // Records an applied correction flagged for review, with why (same transaction as the effect).
  const flagged = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of now.data?.changed.items ?? []) {
      if (item.cause_id) map.set(item.id, item.reason);
    }
    return map;
  }, [now.data]);
  const assumptions = (entries.data?.items ?? []).filter((e) => e.body.category === "assumption");
  useHashTarget(claims.data !== undefined);
  return (
    <>
      <PageHeading
        title="Knowledge"
        lead="Scoped claims, the evidence behind them, and what corrects them."
      />
      <p className="card" data-testid="knowledge-library-link">
        <strong>Library:</strong> papers from your PDF folder, searchable page by page with located
        quotations. <Link to="/knowledge/library">Open the library</Link>
      </p>
      <p className="card" data-testid="knowledge-matrix-link">
        <strong>Evidence matrix:</strong> works compared column by column, every value quoted from an
        exact span and reviewed by you. <Link to="/knowledge/matrix">Open the matrices</Link>
      </p>
      <Search />
      <section aria-labelledby="claims-title" className="section">
        <h2 id="claims-title">Scoped claims</h2>
        <QueryView
          query={claims}
          loadingLabel="Loading claims"
          isEmpty={(d) => d.items.length === 0}
          empty={
            <>
              No claims recorded yet. Claims come from your notes and your agent&apos;s
              findings, each with its scope and evidence.
            </>
          }
        >
          {(data) => (
            <ClaimList
              claims={data.items}
              corrections={corrections.data?.items ?? []}
              flagged={flagged}
            />
          )}
        </QueryView>
      </section>
      <section aria-labelledby="assumptions-title" className="section">
        <h2 id="assumptions-title">Assumptions</h2>
        {assumptions.length === 0 ? (
          <Empty>No assumptions recorded. Claims can point to attributed assumptions they rely on.</Empty>
        ) : (
          <ul className="card-list" data-testid="assumptions">
            {assumptions.map((a) => (
              <AssumptionCard key={a.id} entry={a} flagged={flagged} />
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="corrections-title" className="section">
        <h2 id="corrections-title">Corrections</h2>
        <QueryView
          query={corrections}
          loadingLabel="Loading corrections"
          isEmpty={(d) => d.items.length === 0}
          empty={<>No corrections proposed.</>}
        >
          {(data) => <CorrectionList corrections={data.items} claims={claims.data?.items ?? []} />}
        </QueryView>
      </section>
    </>
  );
}

function Search() {
  const handed = useHandoff("knowledge", (value) => {
    setText(value);
    setQuery(value);
  });
  const [text, setText] = useState(handed);
  const [query, setQuery] = useState(handed);
  const [filter, setFilter] = useState<string>("all");
  const results = useQuery<{ hits: SearchHit[]; mode: string }>(
    query ? `/knowledge/search${q({ q: query, limit: 50 })}` : null,
  );
  const { openSource } = useHub();
  const hits = (results.data?.hits ?? []).filter(
    (h) => h.kind !== "project" && (filter === "all" || h.kind === filter),
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setQuery(text.trim());
  };

  return (
    <section aria-labelledby="search-title" className="section">
      <h2 id="search-title">Search</h2>
      <form role="search" onSubmit={submit} className="search-form" aria-label="Search the project">
        <div className="field grow">
          <label htmlFor="search-q">Search findings, evidence, questions and claims</label>
          <input
            id="search-q"
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
          />
        </div>
        <div className="field">
          <label htmlFor="search-kind">Show</label>
          <select id="search-kind" value={filter} onChange={(e) => setFilter(e.target.value)}>
            {FILTERS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className="button secondary">
          Search
        </button>
      </form>
      {query ? (
        <div aria-live="polite" data-testid="search-results">
          {results.data === undefined && results.loading ? <Loading label="Searching" /> : null}
          {results.error ? <ErrorState error={results.error} onRetry={results.reload} /> : null}
          {results.data ? (
            hits.length === 0 ? (
              <Empty>No matches for “{query}”.</Empty>
            ) : (
              <>
                <p className="muted">
                  {hits.length} result{hits.length === 1 ? "" : "s"} for “{query}”.
                </p>
                <ul className="plain-list results">
                  {hits.map((hit) => {
                    const path = recordPath({ id: hit.id, kind: hit.kind, body: {} });
                    return (
                      <li key={hit.id} className="row result">
                        <span className="badge badge-kind">{kindLabel(hit.kind)}</span>{" "}
                        {hit.kind === "version" ? (
                          <button
                            type="button"
                            className="link-button"
                            onClick={(e) =>
                              openSource({ versionId: hit.id, title: "Source text match" }, e.currentTarget)
                            }
                          >
                            Open source text
                          </button>
                        ) : path && hit.kind !== "item" ? (
                          <Link to={path}>{hit.title || hit.excerpt}</Link>
                        ) : (
                          <span>{hit.title || hit.excerpt}</span>
                        )}
                        {hit.lifecycle !== "active" ? (
                          <span className="muted"> ({hit.lifecycle})</span>
                        ) : null}
                        <p className="excerpt">{hit.excerpt}</p>
                      </li>
                    );
                  })}
                </ul>
              </>
            )
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

const SCOPE_LABELS: [keyof ClaimBody["scope"], string][] = [
  ["objective", "Objective"],
  ["system", "System"],
  ["operating_regime", "Operating regime"],
  ["comparator", "Comparator"],
  ["model_fidelity", "Model fidelity"],
  ["validation_level", "Validation"],
];

function useSourceTitles() {
  const sources = useKindList<SourceBody>("source");
  return useMemo(() => {
    const map = new Map<string, string>();
    for (const s of sources.data?.items ?? []) map.set(s.id, s.body.title);
    return map;
  }, [sources.data]);
}

export function ScopeList({ scope }: { scope: ClaimBody["scope"] }) {
  return (
    <KeyValues
      rows={SCOPE_LABELS.map(([key, label]) => [
        label,
        scope[key] ?? (key === "objective" ? "not recorded" : ""),
      ])}
    />
  );
}

function AssumptionLinks({ id }: { id: string }) {
  const links = useQuery<Links>(`/knowledge/links/${encodeURIComponent(id)}`);
  const relied = links.data?.relies_on ?? [];
  if (relied.length === 0) return null;
  return (
    <div className="assumptions" data-testid="claim-assumptions">
      <h4 className="subhead">Relies on these assumptions</h4>
      <ul className="plain-list">
        {relied.map((a: AssumptionRef) => (
          <li key={a.id} data-assumption-id={a.id} data-assumption-state={a.state}>
            <strong>{a.title}</strong>: {a.text}{" "}
            <span className={a.state === "superseded" ? "badge badge-superseded" : "badge badge-active"}>
              {a.state === "superseded" ? "Superseded" : a.stale ? "Revised since" : "Current"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AssumptionCard({ entry, flagged }: { entry: Envelope<EntryBody>; flagged: Map<string, string> }) {
  const links = useQuery<Links>(`/knowledge/links/${encodeURIComponent(entry.id)}`);
  const successor = (entry.body as { superseded_by?: { id: string } | null }).superseded_by;
  const dependents = links.data?.relied_on_by ?? [];
  return (
    <li
      className="card"
      data-testid="assumption-card"
      data-assumption-state={successor ? "superseded" : "active"}
      id={entry.id}
    >
      <div className="card-head">
        <p className="card-title">{entry.body.title}</p>
        <span className={successor ? "badge badge-superseded" : "badge badge-active"}>
          {successor ? "Superseded" : "Active"}
        </span>
      </div>
      <p>{entry.body.text}</p>
      {successor ? (
        <p className="inline-warning">
          Superseded by <a href={`#${successor.id}`}>another assumption</a>; what relied on this
          one is flagged for review.
        </p>
      ) : null}
      {flagged.has(entry.id) ? <p className="inline-warning">{flagged.get(entry.id)}</p> : null}
      <p className="muted" data-testid="assumption-dependents">
        {dependents.length === 0
          ? "Nothing relies on this assumption yet."
          : `Relied on by ${dependents.length}: ${dependents.map((d) => d.title).join("; ")}.`}
      </p>
    </li>
  );
}

function ClaimList({
  claims,
  corrections,
  flagged,
}: {
  claims: Envelope<ClaimBody>[];
  corrections: Envelope<CorrectionBody>[];
  flagged: Map<string, string>;
}) {
  const titles = useSourceTitles();
  const byId = new Map(claims.map((c) => [c.id, c]));
  const pendingFor = (id: string) =>
    corrections.filter((c) => c.body.status === "proposed" && c.body.target_refs.some((t) => t.id === id));
  return (
    <ol className="card-list" data-testid="claims">
      {claims.map((claim) => {
        const replacement = claim.body.superseded_by ? byId.get(claim.body.superseded_by.id) : undefined;
        const pending = pendingFor(claim.id);
        return (
          <li
            key={claim.id}
            id={claim.id}
            className={`card claim-card claim-${claim.body.state}`}
            data-testid="claim-card"
            data-claim-state={claim.body.state}
          >
            <div className="card-head">
              <p className="card-title">{claim.body.statement}</p>
              <StateBadge state={claim.body.state} prefix="Claim state" />
            </div>
            {replacement ? (
              <p className="inline-warning">
                Superseded by: <Link to={`/knowledge#${replacement.id}`}>{replacement.body.statement}</Link>
              </p>
            ) : null}
            {pending.map((c) => (
              <p key={c.id} className="inline-info">
                A correction is proposed for this claim.{" "}
                <Link to={`/corrections/${c.id}`}>Review the proposed correction</Link>
              </p>
            ))}
            {flagged.has(claim.id) ? (
              <p className="inline-warning" data-testid="claim-flag">
                <span className="badge badge-review">Needs review</span> {humanizeIds(flagged.get(claim.id) ?? "")}
              </p>
            ) : null}
            <h4 className="subhead">Scope</h4>
            <ScopeList scope={claim.body.scope} />
            <AssumptionLinks id={claim.id} />
            <div className="sources">
              <span className="sources-label">Evidence:</span>{" "}
              {claim.body.evidence.length === 0 ? (
                <span className="muted">none cited</span>
              ) : (
                <ul className="inline-list">
                  {claim.body.evidence.map((ev, i) => (
                    <li key={i}>
                      <SourceButton
                        spec={{
                          versionId: ev.version_id,
                          title: titles.get(ev.source_id) ?? "Source",
                          startLine: ev.locator.start_line,
                          endLine: ev.locator.end_line,
                          citedBy: claim.body.statement,
                        }}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <Backlinks id={claim.id} />
            <Details>
              <KeyValues
                rows={[
                  ["Claim", <code key="c">{claim.id}</code>],
                  ["Revision", String(claim.rev)],
                ]}
              />
            </Details>
          </li>
        );
      })}
    </ol>
  );
}

function Backlinks({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [links, setLinks] = useState<Links | null>(null);
  const [error, setError] = useState<unknown>(null);
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !links) {
      try {
        setLinks(await get<Links>(`/knowledge/links/${encodeURIComponent(id)}`));
        setError(null);
      } catch (err) {
        setError(err);
      }
    }
  };
  return (
    <div className="backlinks">
      <button type="button" className="link-button" aria-expanded={open} onClick={() => void toggle()}>
        {open ? "Hide links" : "Show backlinks and links"}
      </button>
      {open ? (
        error ? (
          <ErrorState error={error} />
        ) : links ? (
          <LinkLists links={links} />
        ) : (
          <Loading label="Loading links" />
        )
      ) : null}
    </div>
  );
}

const VIA_LABELS: Record<string, string> = {
  "effects/target_ref": "corrects",
  target_refs: "targets",
  "effects/replacement_ref": "replaces with",
  replacement_refs: "replacement",
  "evidence/source_id": "cites source",
  "evidence/version_id": "cites version",
  input_refs: "used as agent input",
  source_refs: "derived from",
  superseded_by: "superseded by",
  claim_refs: "refers to claim",
};

function LinkLists({ links }: { links: Links }) {
  const dedupe = (list: Links["incoming"]) => {
    const seen = new Map<string, string[]>();
    for (const l of list) seen.set(l.id, [...(seen.get(l.id) ?? []), VIA_LABELS[l.via] ?? l.via]);
    return [...seen.entries()];
  };
  const incoming = dedupe(links.incoming);
  const outgoing = dedupe(links.outgoing).filter(([id]) => kindOf(id) !== "version");
  const records = useRecords([...incoming, ...outgoing].map(([id]) => id));
  const row = ([id, vias]: [string, string[]]) => {
    const r = records.records.get(id);
    const path = r ? recordPath(r) : null;
    const title = r ? recordTitle(r) : kindLabel(kindOf(id));
    return (
      <li key={id}>
        <span className="badge badge-kind">{kindLabel(kindOf(id))}</span>{" "}
        {path ? <Link to={path}>{title}</Link> : <span>{title}</span>}{" "}
        <span className="muted">({[...new Set(vias)].join(", ")})</span>
      </li>
    );
  };
  return (
    <div className="link-lists" data-testid="backlinks">
      <h4 className="subhead">Linked from (backlinks)</h4>
      {incoming.length ? <ul className="plain-list">{incoming.map(row)}</ul> : <p className="muted">Nothing links here.</p>}
      <h4 className="subhead">Links to</h4>
      {outgoing.length ? <ul className="plain-list">{outgoing.map(row)}</ul> : <p className="muted">No outgoing links.</p>}
    </div>
  );
}

function CorrectionList({
  corrections,
  claims,
}: {
  corrections: Envelope<CorrectionBody>[];
  claims: Envelope<ClaimBody>[];
}) {
  const byId = new Map(claims.map((c) => [c.id, c]));
  return (
    <ul className="card-list" data-testid="corrections">
      {corrections.map((c) => {
        const applied = c.body.status === "applied";
        return (
          <li
            key={c.id}
            className={`card correction-card correction-${c.body.status}`}
            data-testid="correction-card"
            data-status={c.body.status}
          >
            <div className="card-head">
              <p className="card-title">
                <span className="correction-label">
                  {applied ? "Applied correction" : c.body.status === "proposed" ? "Proposed correction" : "Rejected correction"}
                </span>
                : {c.body.reason}
              </p>
              <StateBadge state={c.body.status} prefix="Correction" />
            </div>
            <ul className="plain-list">
              {c.body.effects.map((e, i) => (
                <li key={i}>
                  {e.effect === "supersede" ? "Supersedes" : e.effect.replace(/_/g, " ")}: “
                  {byId.get(e.target_ref.id)?.body.statement ?? e.target_ref.id}”
                </li>
              ))}
            </ul>
            <Link to={`/corrections/${c.id}`}>
              {applied ? "View the applied correction" : "Review and decide"}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
