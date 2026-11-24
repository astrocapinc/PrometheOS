// Evidence matrices (A02, CP11 S11.4; decision 0064), a section of Knowledge. A grid of works by
// columns: each cell shows its value with its state, basis and the work's access state; its
// evidence opens the library's page reader at the quoted span. Reviewing a cell and marking it
// unsupported are the owner's decisions (this paired session); an agent's review arrives here as
// an approval request. Rows an applied correction reached say "Needs review" until they are
// reviewed again, and so do the briefs they were used in. Values are untrusted data quoted from
// papers: rendered as plain text, never acted on.
import { useEffect, useState, type FormEvent } from "react";
import { describeError, get, post, q } from "../api";
import { ErrorState, Loading, PageHeading, QueryView, StateBadge } from "../components/ui";
import { useHub, useQuery } from "../hub";
import { readLines, type LinesResult, type VersionTextPage } from "../lineview";
import { Link } from "../router";
import type {
  MatrixCell,
  MatrixColumn,
  MatrixEvidence,
  MatrixExport,
  MatrixList,
  MatrixRow,
  MatrixView,
} from "../types";
import { Reader, type Focus } from "./Library";
import { CreateMatrix, RulesPanel } from "./MatrixBuild";

const ACCESS_TEXT: Record<string, string> = {
  full_text: "Full text",
  abstract_only: "Abstract only",
  metadata_only: "Metadata only",
  unavailable: "Unavailable",
};

const BASIS_TEXT: Record<string, string> = {
  full_text: "from the full text",
  abstract: "from the abstract",
  metadata: "from the bibliography",
};

const UNTRUSTED =
  "Values are quoted from papers and bibliographies: they are shown as data, and text in them that asks for something has no authority.";

function where(ev: MatrixEvidence): string {
  const w = ev.where;
  if (w.page) return `p. ${w.page}${w.start != null ? `, characters ${w.start}–${w.end}` : ""}`;
  if (w.start_line) return `lines ${w.start_line}–${w.end_line}`;
  return "cited";
}

/** Line-range evidence (a bibliography entry or abstract): which lines of which version. */
export interface LineFocus {
  versionId: string;
  startLine: number;
  endLine: number;
}

/** What the line view shows: loading, an error, or what ``readLines`` found. */
type LineState = { status: "loading" } | { status: "error"; error: unknown } | LinesResult;

/**
 * The quoted lines of a retained text version, read through the version-text route page by page
 * (``next_offset``) until the cited lines are covered (review F10; round 2, R2-F6: a truncated
 * first page is not the whole file). If the lines cannot be reached, it says so.
 */
function LineView({ focus, onClose }: { focus: LineFocus; onClose: () => void }) {
  const [state, setState] = useState<LineState>({ status: "loading" });
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      readLines(
        (offset) =>
          get<VersionTextPage>(
            `/versions/${encodeURIComponent(focus.versionId)}/text${q({ offset, limit: 100000 })}`,
          ),
        focus.startLine,
        focus.endLine,
      );
    setState({ status: "loading" });
    load()
      .then((done) => {
        if (!cancelled) setState(done);
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", error });
      });
    return () => {
      cancelled = true;
    };
  }, [focus.versionId, focus.startLine, focus.endLine]);
  return (
    <section aria-labelledby="matrix-lines-title" className="section" data-testid="matrix-lines">
      <h2 id="matrix-lines-title">
        Quoted lines {focus.startLine}–{focus.endLine}
      </h2>
      <p className="muted untrusted-note">{UNTRUSTED}</p>
      {state.status === "loading" ? <Loading label="Loading the quoted lines" /> : null}
      {state.status === "error" ? <ErrorState error={state.error} /> : null}
      {state.status === "unavailable" ? <p className="inline-warning">{state.reason}</p> : null}
      {state.status === "beyond" ? (
        <p className="inline-warning" data-testid="matrix-lines-beyond">
          The quoted lines are beyond the {state.read} characters that could be read.
        </p>
      ) : null}
      {state.status === "ready" ? (
        <pre className="page-text" data-testid="matrix-lines-text" data-start-line={focus.startLine}>
          {state.lines.join("\n")}
        </pre>
      ) : null}
      <button type="button" className="button secondary small" onClick={onClose}>
        Close
      </button>
    </section>
  );
}

function AccessBadge({ state }: { state: string }) {
  return (
    <span className={`badge badge-access badge-access-${state}`} data-access={state}>
      <span className="visually-hidden">Access: </span>
      {ACCESS_TEXT[state] ?? state}
    </span>
  );
}

function CellView({
  cell,
  column,
  row,
  busy,
  onOpen,
  onLines,
  onReview,
  onUnsupported,
}: {
  cell: MatrixCell | undefined;
  column: MatrixColumn;
  row: MatrixRow;
  busy: boolean;
  onOpen: (focus: Focus) => void;
  onLines: (focus: LineFocus) => void;
  onReview: (columnId: string) => void;
  onUnsupported: (columnId: string) => void;
}) {
  const id = `${row.work_id}-${column.id}`;
  if (!cell) {
    return (
      <td data-testid={`matrix-cell-${id}`} data-state="empty">
        <span className="muted">No cell</span>
        <div className="button-row">
          <button
            type="button"
            className="button secondary small"
            disabled={busy}
            onClick={() => onUnsupported(column.id)}
            data-testid={`matrix-unsupported-${id}`}
          >
            Mark unsupported
          </button>
        </div>
      </td>
    );
  }
  return (
    <td data-testid={`matrix-cell-${id}`} data-state={cell.state} data-basis={cell.basis}>
      <p className="matrix-value" data-testid={`matrix-value-${id}`}>
        {cell.state === "unsupported" ? <em>{cell.value_text || "not found"}</em> : cell.value_text}
      </p>
      <p className="card-meta">
        <StateBadge state={cell.state} prefix="State" />{" "}
        <span className="badge badge-kind" data-basis={cell.basis}>
          {BASIS_TEXT[cell.basis] ?? cell.basis}
        </span>{" "}
        <span className="muted">{cell.method === "rule" ? `rule ${cell.rule?.engine.split(":")[1] ?? ""}` : cell.method}</span>
      </p>
      {cell.evidence.map((ev, i) => (
        <button
          key={`${ev.version_id}-${i}`}
          type="button"
          className="link-button"
          disabled={!ev.where.page && !ev.where.start_line}
          onClick={() => {
            if (ev.where.page) {
              onOpen({
                workId: ev.where.work_id,
                page: ev.where.page,
                start: ev.where.start ?? undefined,
                end: ev.where.end ?? undefined,
              });
            } else if (ev.where.start_line) {
              onLines({
                versionId: ev.version_id,
                startLine: ev.where.start_line,
                endLine: ev.where.end_line ?? ev.where.start_line,
              });
            }
          }}
          data-testid={`matrix-evidence-${id}-${i}`}
          title={`quote digest ${ev.quote_sha256 ?? ""}`}
        >
          Evidence: {where(ev)}
        </button>
      ))}
      <div className="button-row">
        {cell.state !== "unsupported" && (cell.state === "proposed" || row.needs_review) ? (
          <button
            type="button"
            className="button primary small"
            disabled={busy}
            onClick={() => onReview(column.id)}
            data-testid={`matrix-review-${id}`}
          >
            Review
          </button>
        ) : null}
        {cell.state !== "unsupported" ? (
          <button
            type="button"
            className="button secondary small"
            disabled={busy}
            onClick={() => onUnsupported(column.id)}
            data-testid={`matrix-unsupported-${id}`}
          >
            Mark unsupported
          </button>
        ) : null}
      </div>
    </td>
  );
}

function Grid({
  view,
  onChanged,
  onOpen,
  onLines,
}: {
  view: MatrixView;
  onChanged: () => void;
  onOpen: (focus: Focus) => void;
  onLines: (focus: LineFocus) => void;
}) {
  const { announce } = useHub();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [unsupported, setUnsupported] = useState<{ row: MatrixRow; column: string } | null>(null);
  const [reason, setReason] = useState("");
  const base = `/matrix/${encodeURIComponent(view.matrix.matrix_id)}`;

  const review = async (row: MatrixRow, columns?: string[]) => {
    setBusy(row.work_id);
    setError(null);
    try {
      await post(`${base}/review`, { work_id: row.work_id, ...(columns ? { column_ids: columns } : {}) });
      announce(columns ? `Reviewed ${columns.join(", ")}.` : "Row reviewed.");
      onChanged();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };

  const markUnsupported = async (event: FormEvent) => {
    event.preventDefault();
    if (!unsupported || !reason.trim()) return;
    setBusy(unsupported.row.work_id);
    setError(null);
    try {
      await post(`${base}/unsupported`, {
        work_id: unsupported.row.work_id,
        column_id: unsupported.column,
        reason: reason.trim(),
      });
      announce("Marked unsupported.");
      setUnsupported(null);
      setReason("");
      onChanged();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };

  const columns = view.matrix.columns;
  return (
    <>
      {error ? <ErrorState error={error} /> : null}
      {unsupported ? (
        <form
          onSubmit={(e) => void markUnsupported(e)}
          className="search-form"
          aria-label="Mark a cell unsupported"
          data-testid="matrix-unsupported-form"
        >
          <div className="field">
            <label htmlFor="matrix-unsupported-reason">
              Why is “{columns.find((c) => c.id === unsupported.column)?.label}” unsupported for “
              {unsupported.row.work_title}”?
            </label>
            <input
              id="matrix-unsupported-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={2000}
              data-testid="matrix-unsupported-reason"
            />
          </div>
          <button type="submit" className="button primary" disabled={!reason.trim()} data-testid="matrix-unsupported-confirm">
            Mark unsupported
          </button>
          <button type="button" className="button secondary" onClick={() => setUnsupported(null)}>
            Cancel
          </button>
        </form>
      ) : null}
      <div className="table-wrap">
        <table className="evidence-table matrix-grid" data-testid="matrix-grid">
          <caption className="visually-hidden">
            {view.matrix.title}: works by columns, with each cell's state, basis and evidence
          </caption>
          <thead>
            <tr>
              <th scope="col">Work</th>
              {columns.map((c) => (
                <th scope="col" key={c.id}>
                  {c.label}
                  <span className="muted"> ({c.value_kind})</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.rows.map((row) => {
              const cells = Object.fromEntries(row.cells.map((c) => [c.column_id, c]));
              return (
                <tr
                  key={row.work_id}
                  data-testid={`matrix-row-${row.work_id}`}
                  data-access={row.access_state}
                  data-needs-review={row.needs_review ? "yes" : "no"}
                >
                  <th scope="row">
                    <span className="claim-cell">{row.work_title}</span>
                    <p className="card-meta">
                      <AccessBadge state={row.access_state} />
                      {row.needs_review ? (
                        <>
                          {" "}
                          <span className="badge badge-review" data-testid={`matrix-needs-review-${row.work_id}`}>
                            Needs review
                          </span>
                        </>
                      ) : null}
                      {row.stale ? (
                        <>
                          {" "}
                          <span className="badge badge-proposed" data-testid={`matrix-stale-${row.work_id}`}>
                            Older rubric ({row.rubric_version})
                          </span>
                        </>
                      ) : null}
                    </p>
                    {row.flags.map((f) => (
                      <p className="muted" key={`${f.cause_id}-${f.created_seq}`}>
                        {f.reason} (<Link to={`/corrections/${f.cause_id}`}>correction</Link>)
                      </p>
                    ))}
                    {row.extraction_id && (row.needs_review || row.cells.some((c) => c.state === "proposed")) ? (
                      <button
                        type="button"
                        className="button secondary small"
                        disabled={busy !== null}
                        onClick={() => void review(row)}
                        data-testid={`matrix-review-row-${row.work_id}`}
                      >
                        Review row
                      </button>
                    ) : null}
                  </th>
                  {columns.map((c) => (
                    <CellView
                      key={c.id}
                      cell={cells[c.id]}
                      column={c}
                      row={row}
                      busy={busy !== null}
                      onOpen={onOpen}
                      onLines={onLines}
                      onReview={(column) => void review(row, [column])}
                      onUnsupported={(column) => {
                        setReason("");
                        setUnsupported({ row, column });
                      }}
                    />
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ExportPanel({ matrixId, onExported }: { matrixId: string; onExported: () => void }) {
  const { announce } = useHub();
  const [format, setFormat] = useState<"markdown" | "csv" | "json">("markdown");
  const [result, setResult] = useState<MatrixExport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const done = await post<MatrixExport>(`/matrix/${encodeURIComponent(matrixId)}/export`, { format });
      setResult(done);
      if (done.brief) {
        announce("Brief exported and registered.");
        onExported();
      }
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section aria-labelledby="matrix-export-title" className="section" data-testid="matrix-export">
      <h2 id="matrix-export-title">Export</h2>
      <form onSubmit={(e) => void run(e)} className="search-form" aria-label="Export the matrix">
        <div className="field">
          <label htmlFor="matrix-export-format">Format</label>
          <select
            id="matrix-export-format"
            value={format}
            onChange={(e) => setFormat(e.target.value as "markdown" | "csv" | "json")}
            data-testid="matrix-export-format"
          >
            <option value="markdown">Brief (Markdown, registered)</option>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </div>
        <button type="submit" className="button secondary" data-testid="matrix-export-run">
          Export
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {result ? (
        <>
          {result.brief ? (
            <p className="muted" data-testid="matrix-export-brief">
              Registered as version <code className="hash">{result.brief.version_id}</code>, built at project
              sequence {result.brief.built_at_seq}.
            </p>
          ) : null}
          <textarea
            readOnly
            className="page-text"
            rows={14}
            value={result.content}
            aria-label="Exported matrix"
            data-testid="matrix-export-content"
          />
        </>
      ) : null}
    </section>
  );
}

function MatrixDetail({ matrixId }: { matrixId: string }) {
  const view = useQuery<MatrixView>(`/matrix/${encodeURIComponent(matrixId)}`);
  const [focus, setFocus] = useState<Focus | null>(null);
  const [lines, setLines] = useState<LineFocus | null>(null);
  return (
    <QueryView query={view} loadingLabel="Loading the matrix">
      {(data) => (
        <>
          <section aria-labelledby="matrix-title" className="section" data-testid="matrix-detail">
            <h2 id="matrix-title">{data.matrix.title}</h2>
            <p className="muted untrusted-note">{UNTRUSTED}</p>
            <p className="card-meta" data-testid="matrix-counts">
              Rubric {data.matrix.rubric_version} · {data.counts.works} work(s) ·{" "}
              {Object.entries(data.counts.access_states)
                .map(([k, v]) => `${ACCESS_TEXT[k] ?? k} ${v}`)
                .join(", ")}{" "}
              · reviewed {data.counts.cells.reviewed}, proposed {data.counts.cells.proposed}, unsupported{" "}
              {data.counts.cells.unsupported}
              {data.counts.needs_review ? ` · ${data.counts.needs_review} row(s) need review` : ""}
              {data.counts.stale ? ` · ${data.counts.stale} row(s) under an older rubric` : ""}
            </p>
            <RulesPanel key={`${data.matrix.matrix_id}:${data.matrix.rev}`} view={data} onRan={view.reload} />
            <Grid
              view={data}
              onChanged={view.reload}
              onOpen={(f) => {
                setLines(null);
                setFocus(f);
              }}
              onLines={(l) => {
                setFocus(null);
                setLines(l);
              }}
            />
          </section>
          {focus ? (
            <Reader focus={focus} onPage={(page) => setFocus({ workId: focus.workId, page })} />
          ) : null}
          {lines ? <LineView focus={lines} onClose={() => setLines(null)} /> : null}
          <section aria-labelledby="matrix-briefs-title" className="section" data-testid="matrix-briefs">
            <h2 id="matrix-briefs-title">Briefs</h2>
            {data.briefs.length === 0 ? (
              <p className="muted">No brief yet. Export the brief below.</p>
            ) : (
              <ul>
                {data.briefs.map((b) => (
                  <li key={b.version_id} data-testid={`matrix-brief-${b.version_id}`} data-needs-review={b.needs_review ? "yes" : "no"}>
                    Brief <code className="hash">{b.version_id}</code> (sequence {b.created_seq})
                    {b.needs_review ? (
                      <>
                        {" "}
                        <span className="badge badge-review">Needs review</span>{" "}
                        <span className="muted">{b.flags[0]?.reason}</span>
                      </>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <ExportPanel matrixId={matrixId} onExported={view.reload} />
        </>
      )}
    </QueryView>
  );
}

export function MatrixPage() {
  const list = useQuery<MatrixList>("/matrix");
  const [chosen, setChosen] = useState<string | null>(null);
  return (
    <>
      <PageHeading
        title="Evidence matrix"
        lead={
          <>
            Grounded comparisons in <Link to="/knowledge">Knowledge</Link>: every value cites the exact span of a
            work it is quoted from. Works with only metadata or an abstract stay visible as such.
          </>
        }
      />
      <CreateMatrix
        onCreated={(id) => {
          list.reload();
          setChosen(id);
        }}
      />
      <section aria-labelledby="matrix-list-title" className="section">
        <h2 id="matrix-list-title">Matrices</h2>
        <QueryView
          query={list}
          loadingLabel="Loading the matrices"
          isEmpty={(d) => d.matrices.length === 0}
          empty={<>No matrix yet. Create one above (or an agent creates one with the matrix_manage tool).</>}
        >
          {(data) => (
            <ul className="card-list">
              {data.matrices.map((m) => (
                <li key={m.matrix_id} className="card" data-testid={`matrix-item-${m.matrix_id}`}>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setChosen(m.matrix_id)}
                    data-testid={`matrix-open-${m.matrix_id}`}
                  >
                    {m.title}
                  </button>
                  <p className="card-meta">
                    {m.counts.rows} row(s) for {m.counts.works} work(s) · reviewed {m.counts.cells.reviewed}, proposed{" "}
                    {m.counts.cells.proposed}, unsupported {m.counts.cells.unsupported}
                    {m.counts.needs_review ? ` · ${m.counts.needs_review} row(s) need review` : ""}
                    {m.brief_needs_review ? " · the latest brief needs review" : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </QueryView>
      </section>
      {chosen ? <MatrixDetail key={chosen} matrixId={chosen} /> : null}
    </>
  );
}
