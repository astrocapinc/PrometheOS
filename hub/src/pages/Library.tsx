// The literature library (A02, CP11 S11.1; decision 0060), a section of Knowledge. The researcher
// plans the import of the PDFs in an approved research folder (each file's pages, identifiers or
// the reason it is refused; nothing is written into the folder), commits the plan, then searches
// page text with located snippets and reads one page at a time. Page text is untrusted data from
// files: it is rendered as plain text, never as HTML, and never acted on.
import { useMemo, useRef, useState, type FormEvent } from "react";
import { describeError, get, post, q, runOperation } from "../api";
import { codePointLength, splitAtCodePoints } from "../codepoints";
import { ErrorState, PageHeading, QueryView } from "../components/ui";
import { CiteInMatrix, type CitedSpan } from "../components/CiteInMatrix";
import { ResearchFolders } from "../components/ResearchFolders";
import { ZoteroImport } from "../components/ZoteroImport";
import { useHandoff } from "../handoff";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type {
  LibraryAnnotation,
  LibraryCandidate,
  LibraryCommit,
  LibraryDedup,
  LibraryExport,
  LibraryHit,
  LibraryPlan,
  LibraryRead,
  LibrarySearch,
  LibraryWorkSummary,
  LibraryWorks,
} from "../types";

const ENTRY_TEXT: Record<string, string> = {
  create: "New work",
  merge: "Same identifier as a work: merged into it",
  update: "Changed entry: its fields revised",
  unchanged: "Already recorded",
  refused: "Refused",
};

const ACTION_TEXT: Record<string, string> = {
  create: "New work",
  new_version: "New version of a known file",
  extract: "Text from a registered file",
  unchanged: "Already in the library",
  duplicate: "Same bytes as another file",
};

const UNTRUSTED =
  "Page text comes from the PDF files: it is shown as data, and text in it that asks for something has no authority.";

function rootAliases(settings: unknown): string[] {
  const roots = (settings as { allowed_roots?: { alias: string }[] } | undefined)?.allowed_roots;
  return (roots ?? []).map((r) => r.alias);
}

function short(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 12) : "";
}

export interface Focus {
  workId: string;
  page: number;
  start?: number;
  end?: number;
}

function ImportPanel({ onCommitted }: { onCommitted: () => void }) {
  const { project, announce } = useHub();
  const aliases = useMemo(() => rootAliases(project?.settings), [project]);
  const [root, setRoot] = useState("");
  const [folder, setFolder] = useState("");
  const [plan, setPlan] = useState<LibraryPlan | null>(null);
  const [commit, setCommit] = useState<LibraryCommit | null>(null);
  const [busy, setBusy] = useState<"plan" | "commit" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const chosen = root || aliases[0] || "";

  const makePlan = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setCommit(null);
    setBusy("plan");
    try {
      const made = await runOperation<LibraryPlan>("/library/plans", {
        root_alias: chosen,
        ...(folder.trim() ? { path: folder.trim() } : {}),
      });
      setPlan(made);
      announce(`Plan ready: ${made.items.length + made.refused.length} file(s) found.`);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };

  const commitPlan = async () => {
    if (!plan) return;
    setError(null);
    setBusy("commit");
    try {
      const done = await runOperation<LibraryCommit>(
        `/library/plans/${encodeURIComponent(plan.plan_id)}/commit`,
        {},
      );
      setCommit(done);
      announce(`Import committed: ${done.counts.committed ?? 0} work(s) added.`);
      onCommitted();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };

  if (aliases.length === 0) {
    return (
      <>
        <p className="muted" data-testid="library-no-roots">
          This project has no approved research folder yet. Add the folder that holds your files under
          Research folders below.
        </p>
        <ResearchFolders open />
      </>
    );
  }
  const actionable = plan
    ? plan.items.filter((i) => ["create", "new_version", "extract"].includes(i.action)).length
    : 0;
  return (
    <section className="card" aria-labelledby="library-import-heading">
      <h2 id="library-import-heading">Import PDFs and bibliographies from a research folder</h2>
      <p className="muted">
        Reading only: nothing is written into the folder. Each PDF is kept, its page text is extracted
        (no OCR: scanned pages have no text), and files with the same bytes are imported once.
      </p>
      <form onSubmit={(e) => void makePlan(e)} className="search-form" aria-label="Plan a library import">
        <div className="field">
          <label htmlFor="library-root">Folder</label>
          <select id="library-root" value={chosen} onChange={(e) => setRoot(e.target.value)} data-testid="library-root">
            {aliases.map((alias) => (
              <option key={alias} value={alias}>
                {alias}
              </option>
            ))}
          </select>
        </div>
        <div className="field grow">
          <label htmlFor="library-path">Only this sub-folder or file (optional)</label>
          <input
            id="library-path"
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="papers/2026"
            maxLength={1024}
            data-testid="library-path"
          />
        </div>
        <button type="submit" className="button secondary" disabled={busy !== null} data-testid="library-plan">
          {busy === "plan" ? "Planning…" : "Plan import"}
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {plan ? (
        <div data-testid="library-plan-result" data-plan-id={plan.plan_id}>
          <h3>Plan</h3>
          <p data-testid="library-plan-counts">
            {plan.counts.create ?? 0} new, {plan.counts.new_version ?? 0} changed,{" "}
            {plan.counts.extract ?? 0} to extract, {plan.counts.unchanged ?? 0} already imported,{" "}
            {plan.counts.duplicate ?? 0} duplicate, {plan.refused.length} refused
            {plan.more ? `; ${plan.more} more PDF(s) left for a later plan` : ""}.
          </p>
          <div className="table-wrap">
            <table className="data-table files-table" data-testid="library-plan-items">
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">Action</th>
                  <th scope="col">Title (where from)</th>
                  <th scope="col">Pages</th>
                  <th scope="col">Identifiers</th>
                </tr>
              </thead>
              <tbody>
                {plan.items.map((item) => (
                  <tr key={item.path} data-action={item.action} data-kind={item.kind ?? "pdf"}>
                    <td>
                      <code>{item.path}</code>
                    </td>
                    <td>
                      {item.kind === "bibliography" && item.action === "unchanged"
                        ? "Nothing new to record"
                        : (ACTION_TEXT[item.action] ?? item.action)}
                      {item.duplicate_of ? (
                        <>
                          {" "}
                          (<code>{item.duplicate_of}</code>)
                        </>
                      ) : null}
                    </td>
                    <td>
                      {item.kind === "bibliography" ? (
                        <BibliographyEntries item={item} />
                      ) : (
                        <>
                          {item.title ?? ""}
                          {item.title_basis ? <span className="muted"> ({item.title_basis.replace(/_/g, " ")})</span> : null}
                        </>
                      )}
                    </td>
                    <td>
                      {item.page_count ?? ""}
                      {item.pages_without_text && item.pages_without_text.length > 0 ? (
                        <span className="muted"> (no text on {item.pages_without_text.join(", ")})</span>
                      ) : null}
                    </td>
                    <td>
                      {item.doi ? <div>DOI {item.doi}</div> : null}
                      {item.arxiv ? <div>arXiv {item.arxiv}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {plan.refused.length > 0 ? (
            <>
              <h3>Refused</h3>
              <ul className="plain-list" data-testid="library-refused">
                {plan.refused.map((r) => (
                  <li key={r.path} data-reason={r.reason}>
                    <code>{r.path}</code>: {r.detail}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <div className="button-row">
            <button
              type="button"
              className="button primary"
              disabled={busy !== null || actionable === 0 || commit !== null}
              onClick={() => void commitPlan()}
              data-testid="library-commit"
            >
              {busy === "commit" ? "Importing…" : `Import ${actionable} file(s)`}
            </button>
          </div>
        </div>
      ) : null}
      {commit ? (
        <p data-testid="library-commit-result" role="status">
          Imported {commit.counts.committed ?? 0} file(s); {commit.counts.skipped ?? 0} skipped,{" "}
          {commit.counts.unchanged ?? 0} already imported, {commit.counts.duplicate ?? 0} duplicate,{" "}
          {commit.counts.refused ?? 0} refused.
        </p>
      ) : null}
    </section>
  );
}

function BibliographyEntries({ item }: { item: LibraryPlan["items"][number] }) {
  const counts = item.entry_counts ?? {};
  return (
    <details data-testid="library-bib-entries">
      <summary>
        {item.format?.toUpperCase()} file: {item.entries?.length ?? 0} entries ({counts.create ?? 0} new,{" "}
        {counts.merge ?? 0} merged, {counts.update ?? 0} changed, {counts.unchanged ?? 0} recorded,{" "}
        {counts.refused ?? 0} refused)
      </summary>
      {item.unmapped_columns && item.unmapped_columns.length > 0 ? (
        <p className="muted">Columns not imported: {item.unmapped_columns.join(", ")}</p>
      ) : null}
      <ul className="plain-list">
        {(item.entries ?? []).map((e) => (
          <li key={`${e.key}:${e.lines[0]}`} data-entry-action={e.action}>
            <code>{e.key}</code> (lines {e.lines[0]}–{e.lines[1]}): {ENTRY_TEXT[e.action] ?? e.action}
            {e.title ? <> · {e.title}</> : null}
            {e.reason ? <span className="muted"> · {e.reason}</span> : null}
            {e.refused_fields.length > 0 ? (
              <span className="muted">
                {" "}
                · fields refused: {e.refused_fields.map((f) => `${f.field} (${f.reason})`).join("; ")}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

function WorksList({ works, onOpen }: { works: LibraryWorkSummary[]; onOpen: (f: Focus) => void }) {
  return (
    <ul className="card-list" data-testid="library-works">
      {works.map((w) => (
        <li key={w.work_id} className="card" data-work-id={w.work_id}>
          <div className="card-head">
            <p className="card-title">{w.title}</p>
            {w.page_count !== null ? (
              <button type="button" className="button secondary small" onClick={() => onOpen({ workId: w.work_id, page: 1 })}>
                Read
              </button>
            ) : (
              <span className="muted">No full text</span>
            )}
          </div>
          <p className="card-meta">
            {w.contributors.length > 0 ? `${w.contributors.join("; ")} · ` : ""}
            {w.year !== null ? `${w.year} · ` : ""}
            {w.venue ? `${w.venue} · ` : ""}
            {w.page_count !== null ? `${w.page_count} page(s)` : "bibliography only"}
            {w.pages_without_text > 0 ? `, ${w.pages_without_text} without text` : ""}
            {w.access_state !== "full_text" ? ` · ${w.access_state.replace(/_/g, " ")}` : ""}
          </p>
          <p className="card-meta">
            {w.identifiers.doi ? <>DOI {w.identifiers.doi} </> : null}
            {w.identifiers.arxiv ? <>arXiv {w.identifiers.arxiv} </> : null}
            {w.identifiers.pmid ? <>PMID {w.identifiers.pmid} </> : null}
            {w.identifiers.pmcid ? <>{w.identifiers.pmcid} </> : null}
            {w.identifiers.isbn ? <>ISBN {w.identifiers.isbn} </> : null}
            {w.sources > 0 ? <span className="muted">{w.sources} bibliography source(s) </span> : null}
            {w.file ? (
              <span className="muted">
                {w.file.alias}: <code>{w.file.path}</code>
              </span>
            ) : null}
          </p>
        </li>
      ))}
    </ul>
  );
}

function SearchPanel({ onOpen, onCite }: { onOpen: (f: Focus) => void; onCite: (span: CitedSpan) => void }) {
  const handed = useHandoff("library", (value) => {
    setText(value);
    setQuery(value);
  });
  const [text, setText] = useState(handed);
  const [query, setQuery] = useState(handed);
  const results = useQuery<LibrarySearch>(query ? `/library/search${q({ query, limit: 20 })}` : null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setQuery(text.trim());
  };
  return (
    <section aria-labelledby="library-search-title" className="section">
      <h2 id="library-search-title">Search page text</h2>
      <form role="search" onSubmit={submit} className="search-form" aria-label="Search the library">
        <div className="field grow">
          <label htmlFor="library-q">Words to find on one page</label>
          <input
            id="library-q"
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
            data-testid="library-query"
          />
        </div>
        <button type="submit" className="button secondary" data-testid="library-search">
          Search
        </button>
      </form>
      {query ? (
        <div aria-live="polite" data-testid="library-hits">
          <QueryView
            query={results}
            loadingLabel="Searching"
            isEmpty={(d) => d.hits.length === 0}
            empty={<>No page mentions “{query}”.</>}
          >
            {(data) => (
              <>
                <p className="muted untrusted-note">{UNTRUSTED}</p>
                <ul className="plain-list results">
                  {data.hits.map((hit: LibraryHit) => (
                    <li key={`${hit.text_version_id}:${hit.page}:${hit.start}`} className="card" data-page={hit.page}>
                      <p className="card-title">{hit.title}</p>
                      <p className="hit-snippet">
                        {hit.snippet.before}
                        <mark>{hit.snippet.match}</mark>
                        {hit.snippet.after}
                      </p>
                      <p className="card-meta">
                        Page {hit.page}, characters {hit.start}–{hit.end} · quote digest{" "}
                        <code className="hash">{short(hit.quote_sha256)}</code>{" "}
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => onOpen({ workId: hit.work_id, page: hit.page, start: hit.start, end: hit.end })}
                        >
                          Open page {hit.page}
                        </button>{" "}
                        <button
                          type="button"
                          className="link-button"
                          data-testid="library-cite"
                          onClick={() =>
                            onCite({
                              workId: hit.work_id,
                              page: hit.page,
                              start: hit.start,
                              end: hit.end,
                              quote: hit.snippet.match,
                              evidence: hit.evidence,
                            })
                          }
                        >
                          Cite in matrix
                        </button>
                      </p>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </QueryView>
        </div>
      ) : null}
    </section>
  );
}

/** ``text`` with the code-point span ``[start, end)`` marked (offsets are code points, not
 *  UTF-16 units; see codepoints.ts). */
function MarkedText({ text, start, end }: { text: string; start: number; end: number }) {
  const [before, span, after] = splitAtCodePoints(text, start, end);
  return (
    <>
      {before}
      <mark>{span}</mark>
      {after}
    </>
  );
}

function Annotations({ focus, page }: { focus: Focus; page: number }) {
  const { announce } = useHub();
  const list = useQuery<{ annotations: LibraryAnnotation[] }>(
    `/library/works/${encodeURIComponent(focus.workId)}/annotations`,
  );
  const [text, setText] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const spanned = focus.start !== undefined && focus.end !== undefined && focus.page === page;
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!spanned) return;
    setBusy(true);
    setError(null);
    try {
      await post("/library/annotations", {
        work_id: focus.workId,
        page,
        start: focus.start,
        end: focus.end,
        text: text.trim(),
      });
      setText("");
      announce("Annotation recorded.");
      list.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const here = (list.data?.annotations ?? []).filter((a) => a.page === page);
  return (
    <div data-testid="library-annotations">
      <h3>Annotations on this page</h3>
      {here.length === 0 ? <p className="muted">None yet.</p> : null}
      <ul className="plain-list">
        {here.map((a) => (
          <li key={a.entry_id} className="card" data-entry-id={a.entry_id}>
            <p>{a.text}</p>
            <p className="card-meta">
              Characters {a.start}–{a.end} · digest <code className="hash">{short(a.quote_sha256)}</code>
            </p>
          </li>
        ))}
      </ul>
      {spanned ? (
        <form onSubmit={(e) => void add(e)} aria-label="Annotate the marked span">
          <div className="field grow">
            <label htmlFor="library-annotation">Annotate the marked span</label>
            <textarea
              id="library-annotation"
              rows={2}
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={20000}
              data-testid="library-annotation-text"
            />
          </div>
          <button type="submit" className="button secondary" disabled={busy || !text.trim()} data-testid="library-annotate">
            Add annotation
          </button>
          {error ? <ErrorState error={error} /> : null}
        </form>
      ) : (
        <p className="muted">Open a search hit to annotate its span.</p>
      )}
    </div>
  );
}

export function Reader({
  focus,
  onPage,
  onCite,
}: {
  focus: Focus;
  onPage: (page: number) => void;
  onCite?: (span: CitedSpan) => void;
}) {
  const preRef = useRef<HTMLPreElement>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  // The chunk that contains the hit (offsets in code points), so a hit beyond the first 8,000
  // characters of a long page is shown and marked too (review F4).
  const from = focus.start !== undefined ? Math.max(0, focus.start - 2000) : undefined;
  const read = useQuery<LibraryRead>(
    `/library/works/${encodeURIComponent(focus.workId)}/read${q({ page: focus.page, start: from, max_chars: 8000 })}`,
  );
  return (
    <section aria-labelledby="library-reader-title" className="section" data-testid="library-reader">
      <QueryView query={read} loadingLabel="Loading the page">
        {(page) => {
          const mark =
            focus.start !== undefined && focus.end !== undefined && focus.end <= page.end && focus.start >= page.start
              ? { start: focus.start - page.start, end: focus.end - page.start }
              : null;
          return (
            <>
              <h2 id="library-reader-title">
                {page.title ?? "Extracted text"}: page {page.page} of {page.page_count}
              </h2>
              <p className="muted untrusted-note">{UNTRUSTED}</p>
              {page.note ? (
                <p className="inline-warning" data-testid="library-page-note">
                  {page.note}
                </p>
              ) : null}
              <pre ref={preRef} className="page-text" data-testid="library-page-text" data-page={page.page}>
                {mark ? (
                  <MarkedText text={page.text} start={mark.start} end={mark.end} />
                ) : (
                  page.text
                )}
              </pre>
              <p className="card-meta">
                Characters {page.start}–{page.end} of {page.page_chars}
                {page.quote_sha256 ? (
                  <>
                    {" "}
                    · digest <code className="hash">{short(page.quote_sha256)}</code>
                  </>
                ) : null}
                {page.clamped || (page.next && page.next.page === page.page) ? " · the page continues" : ""}
              </p>
              {onCite ? (
                <p className="button-row">
                  <button
                    type="button"
                    className="button secondary small"
                    data-testid="library-cite-selection"
                    onClick={() => {
                      // The selected text's place on the page, in code points (the server's
                      // offsets); the server then gives the span's evidence and digest.
                      const sel = window.getSelection();
                      const pre = preRef.current;
                      if (!sel || sel.rangeCount === 0 || !pre || sel.isCollapsed) {
                        setSelectionError("Select text on the page first.");
                        return;
                      }
                      const range = sel.getRangeAt(0);
                      if (!pre.contains(range.startContainer) || !pre.contains(range.endContainer)) {
                        setSelectionError("Select text on the page first.");
                        return;
                      }
                      const before = document.createRange();
                      before.selectNodeContents(pre);
                      before.setEnd(range.startContainer, range.startOffset);
                      const start = page.start + codePointLength(before.toString());
                      const end = start + codePointLength(range.toString());
                      setSelectionError(null);
                      void get<LibraryRead>(
                        `/library/works/${encodeURIComponent(focus.workId)}/read${q({ page: page.page, start, end })}`,
                      )
                        .then((read) =>
                          onCite({
                            workId: focus.workId,
                            page: page.page,
                            start,
                            end,
                            quote: read.text,
                            evidence: read.evidence,
                          }),
                        )
                        .catch((err: unknown) => setSelectionError(describeError(err).detail));
                    }}
                  >
                    Cite selection in matrix
                  </button>
                  {selectionError ? <span className="inline-warning">{selectionError}</span> : null}
                </p>
              ) : null}
              <Annotations focus={focus} page={page.page} />
              <div className="button-row">
                <button
                  type="button"
                  className="button secondary"
                  disabled={page.page <= 1}
                  onClick={() => onPage(page.page - 1)}
                  data-testid="library-prev"
                >
                  Previous page
                </button>
                <button
                  type="button"
                  className="button secondary"
                  disabled={page.page >= page.page_count}
                  onClick={() => onPage(page.page + 1)}
                  data-testid="library-next"
                >
                  Next page
                </button>
              </div>
            </>
          );
        }}
      </QueryView>
    </section>
  );
}

const CHOICE_TEXT: Record<string, string> = {
  merge: "Merged",
  distinct: "Kept separate",
  versions: "Related versions",
  unlinked: "Version link undone (not relinked by imports)",
};

function Candidates({ onResolved }: { onResolved: () => void }) {
  const { announce } = useHub();
  const dedup = useQuery<LibraryDedup>("/library/dedup");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // A choice is confirmed before it is recorded (review round 1): nothing is one click.
  const [pending, setPending] = useState<{ id: string; choice: "merge" | "distinct" | "versions" } | null>(null);
  const undo = async (workA: string, workB: string, what = "Decision undone.") => {
    setBusy(`${workA}~${workB}`);
    setError(null);
    try {
      await post("/library/dedup/undo", { work_a: workA, work_b: workB });
      announce(what);
      dedup.reload();
      onResolved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };
  // An undone import link is not a dead end (review round 2, R2-F2): link it again at once.
  const relink = async (workA: string, workB: string) => {
    setBusy(`${workA}~${workB}`);
    setError(null);
    try {
      await post("/library/dedup/resolve", { work_a: workA, work_b: workB, choice: "versions" });
      announce("Related versions.");
      dedup.reload();
      onResolved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };
  const resolve = async (candidate: LibraryCandidate, choice: "merge" | "distinct" | "versions") => {
    const [a, b] = candidate.works;
    if (!a || !b) return;
    setPending(null);
    setBusy(candidate.candidate_id);
    setError(null);
    try {
      await post("/library/dedup/resolve", { work_a: a.work_id, work_b: b.work_id, choice });
      announce(`${CHOICE_TEXT[choice]}.`);
      dedup.reload();
      onResolved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };
  return (
    <section aria-labelledby="library-dedup-title" className="section" data-testid="library-dedup">
      <h2 id="library-dedup-title">Possible duplicates and versions</h2>
      <p className="muted">
        Works with the same identifier are merged at import. These only look alike (title, first author, year):
        nothing changes until you choose.
      </p>
      {error ? <ErrorState error={error} /> : null}
      <QueryView
        query={dedup}
        loadingLabel="Looking for similar works"
        isEmpty={(d) => d.candidates.length === 0}
        empty={<>No candidates.</>}
      >
        {(data) => (
          <ul className="card-list">
            {data.candidates.map((c) => (
              <li key={c.candidate_id} className="card" data-testid="library-candidate" data-kind={c.kind}>
                <p className="card-title">{c.kind === "version" ? "Possible versions" : "Possible duplicate"}</p>
                <ul className="plain-list">
                  {c.works.map((w) => (
                    <li key={w.work_id}>
                      <strong>{w.title}</strong>
                      <span className="muted">
                        {" "}
                        · {w.first_author ?? "no author"} · {w.year ?? "no year"}
                        {w.venue ? ` · ${w.venue}` : ""}
                        {Object.entries(w.identifiers)
                          .map(([k, v]) => ` · ${k} ${v}`)
                          .join("")}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="card-meta">
                  Same title words “{c.evidence.same_title_key}”, first author {c.evidence.first_author_family}, years{" "}
                  {c.evidence.years.map((y) => y ?? "?").join(" and ")}.
                </p>
                {pending && pending.id === c.candidate_id ? (
                  <div className="inline-warning" data-testid="library-confirm" role="group" aria-label="Confirm the choice">
                    <p>
                      {pending.choice === "merge"
                        ? "Merge: the newer work is marked as merged into the older one (it can be undone)."
                        : pending.choice === "distinct"
                          ? "Keep separate: the pair is not proposed again (it can be undone)."
                          : "Related versions: the preprint is linked to its published version (it can be undone)."}
                    </p>
                    <div className="button-row">
                      <button type="button" className="button primary" disabled={busy !== null} onClick={() => void resolve(c, pending.choice)} data-testid="library-confirm-yes">
                        Confirm
                      </button>
                      <button type="button" className="button secondary" onClick={() => setPending(null)} data-testid="library-confirm-no">
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="button-row">
                    <button type="button" className="button secondary" disabled={busy !== null} onClick={() => setPending({ id: c.candidate_id, choice: "merge" })} data-testid="library-merge">
                      Merge
                    </button>
                    <button type="button" className="button secondary" disabled={busy !== null} onClick={() => setPending({ id: c.candidate_id, choice: "distinct" })} data-testid="library-distinct">
                      Keep separate
                    </button>
                    <button type="button" className="button secondary" disabled={busy !== null} onClick={() => setPending({ id: c.candidate_id, choice: "versions" })} data-testid="library-versions">
                      Related versions
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </QueryView>
      {dedup.data && dedup.data.decisions.length > 0 ? (
        <div data-testid="library-decisions">
          <h3>Decisions</h3>
          <ul className="plain-list">
            {dedup.data.decisions.map((d) => (
              <li key={d.decision_id} data-choice={d.choice}>
                {CHOICE_TEXT[d.choice] ?? d.choice} · <code>{d.work_a}</code> and <code>{d.work_b}</code>
                {d.note ? <span className="muted"> · {d.note}</span> : null}{" "}
                {d.choice === "unlinked" ? (
                  <>
                    <button type="button" className="link-button" disabled={busy !== null} onClick={() => void relink(d.work_a, d.work_b)} data-testid="library-relink">
                      Link again
                    </button>{" "}
                    <button type="button" className="link-button" disabled={busy !== null} onClick={() => void undo(d.work_a, d.work_b, "Marker forgotten: a later import may link the pair.")} data-testid="library-undo">
                      Forget
                    </button>
                  </>
                ) : (
                  <button type="button" className="link-button" disabled={busy !== null} onClick={() => void undo(d.work_a, d.work_b)} data-testid="library-undo">
                    Undo
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {dedup.data && dedup.data.links.some((l) => l.origin === "import") ? (
        <div data-testid="library-links">
          <h3>Version links from imports</h3>
          <p className="muted">An entry named the other work's identifier. Undo a wrong link: later imports do not relink it.</p>
          <ul className="plain-list">
            {dedup.data.links
              .filter((l) => l.origin === "import")
              .map((l) => (
                <li key={`${l.preprint}~${l.published}`} data-testid="library-link">
                  <strong>{l.preprint_title}</strong> <span className="muted">(preprint)</span> is a version of{" "}
                  <strong>{l.published_title}</strong>{" "}
                  <button type="button" className="link-button" disabled={busy !== null} onClick={() => void undo(l.preprint, l.published, "Version link undone.")} data-testid="library-link-undo">
                    Undo
                  </button>
                </li>
              ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function ExportPanel() {
  const [format, setFormat] = useState<"bibtex" | "ris" | "csv">("bibtex");
  const [result, setResult] = useState<LibraryExport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      setResult(await post<LibraryExport>("/library/export", { format }));
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section aria-labelledby="library-export-title" className="section" data-testid="library-export">
      <h2 id="library-export-title">Export</h2>
      <form onSubmit={(e) => void run(e)} className="search-form" aria-label="Export the library">
        <div className="field">
          <label htmlFor="library-export-format">Format</label>
          <select
            id="library-export-format"
            value={format}
            onChange={(e) => setFormat(e.target.value as "bibtex" | "ris" | "csv")}
            data-testid="library-export-format"
          >
            <option value="bibtex">BibTeX</option>
            <option value="ris">RIS</option>
            <option value="csv">CSV</option>
          </select>
        </div>
        <button type="submit" className="button secondary" data-testid="library-export-run">
          Export
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {result ? (
        <>
          <p className="muted">
            {result.count} work(s), stable keys and annotations included. Importing this file again creates nothing.
          </p>
          <textarea
            readOnly
            className="page-text"
            rows={12}
            value={result.content}
            aria-label="Exported bibliography"
            data-testid="library-export-content"
          />
        </>
      ) : null}
    </section>
  );
}

export function LibraryPage() {
  const works = useQuery<LibraryWorks>("/library/works?limit=200");
  const [focus, setFocus] = useState<Focus | null>(null);
  const { project } = useHub();
  const hasRoots = rootAliases(project?.settings).length > 0;
  const [cite, setCite] = useState<CitedSpan | null>(null);
  return (
    <>
      <PageHeading
        title="Library"
        lead={
          <>
            Literature in <Link to="/knowledge">Knowledge</Link>: PDFs from an approved folder, kept as
            sources, with located page text for search, reading and checked quotations.
          </>
        }
      />
      {hasRoots ? <ResearchFolders /> : null}
      <ImportPanel onCommitted={works.reload} />
      <ZoteroImport onCommitted={works.reload} />
      <SearchPanel onOpen={setFocus} onCite={setCite} />
      <Candidates onResolved={works.reload} />
      {cite ? <CiteInMatrix key={`${cite.workId}:${cite.page}:${cite.start}:${cite.end}`} span={cite} onClose={() => setCite(null)} /> : null}
      {focus ? (
        <Reader focus={focus} onPage={(page) => setFocus({ workId: focus.workId, page })} onCite={setCite} />
      ) : null}
      <section aria-labelledby="library-works-title" className="section">
        <h2 id="library-works-title">Works</h2>
        <QueryView
          query={works}
          loadingLabel="Loading the library"
          isEmpty={(d) => d.works.length === 0}
          empty={<>No works yet. Plan an import above.</>}
        >
          {(data) => (
            <>
              <p className="muted" data-testid="library-total">
                {data.total} work(s).
              </p>
              <WorksList works={data.works} onOpen={setFocus} />
            </>
          )}
        </QueryView>
      </section>
      <ExportPanel />
    </>
  );
}
