// "Cite in matrix" (A02, CP11 S11.4b; decision 0066 §2): a quoted span of a work (a search hit
// or a selection in the page reader) becomes a proposed cell, method manual, with the span as
// its evidence. The server grounds it (the span, the quote digest, every number in the value);
// a refusal is shown verbatim. The owner then reviews the cell in the matrix as usual.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, post } from "../api";
import { useHub, useQuery } from "../hub";
import type { MatrixList, MatrixView } from "../types";

export interface CitedSpan {
  workId: string;
  page: number;
  start: number;
  end: number;
  quote: string;
  evidence: unknown;
}

function Refusal({ error }: { error: unknown }) {
  const code = error instanceof ApiError ? error.code : "error";
  const message = error instanceof Error ? error.message : String(error);
  return (
    <p className="inline-warning" role="alert" data-testid="cite-refusal" data-code={code}>
      <strong>Refused ({code}):</strong> {message}
    </p>
  );
}

export function CiteInMatrix({ span, onClose }: { span: CitedSpan; onClose: () => void }) {
  const { announce } = useHub();
  const matrices = useQuery<MatrixList>("/matrix");
  const [matrixId, setMatrixId] = useState<string>("");
  const chosen = matrixId || matrices.data?.matrices[0]?.matrix_id || "";
  const view = useQuery<MatrixView>(chosen ? `/matrix/${encodeURIComponent(chosen)}` : null);
  const columns = view.data?.matrix.columns ?? [];
  const [columnId, setColumnId] = useState("");
  const column = columns.find((c) => c.id === columnId) ?? columns[0];
  const [value, setValue] = useState(span.quote);
  const [decimal, setDecimal] = useState("");
  const [unit, setUnit] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const inMatrix = view.data?.rows.some((r) => r.work_id === span.workId) ?? false;

  // A new column starts from a clean form: a choice column's first choice, otherwise the quote;
  // the number and unit of a quantity column are cleared (S11.4b review F7: no stale value).
  useEffect(() => {
    setValue(column?.value_kind === "choice" ? (column.choices?.[0] ?? "") : span.quote);
    setDecimal("");
    setUnit("");
  }, [column?.id, column?.value_kind, column?.choices, span.quote]);

  // The form opens above the reader: bring it into view when a span is cited.
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.scrollIntoView?.({ block: "nearest" });
  }, [span.workId, span.page, span.start, span.end]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!chosen || !column) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const quantities = column.value_kind === "quantity" && decimal.trim() ? [{ decimal: decimal.trim(), unit: unit.trim() }] : [];
      await post(`/matrix/${encodeURIComponent(chosen)}/propose`, {
        work_id: span.workId,
        column_id: column.id,
        value_text: value,
        evidence: [span.evidence],
        quantities,
      });
      setDone(`Proposed in "${view.data?.matrix.title ?? "the matrix"}", column ${column.label}.`);
      announce("Cell proposed; review it in the matrix.");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section ref={panel} className="card cite-in-matrix" aria-labelledby="cite-title" data-testid="cite-in-matrix">
      <h3 id="cite-title">Cite in matrix</h3>
      <p className="muted">
        Page {span.page}, characters {span.start}–{span.end}: “{span.quote}”
      </p>
      {matrices.data && matrices.data.matrices.length === 0 ? (
        <p className="muted">No matrix yet: create one under Knowledge, Evidence matrix.</p>
      ) : (
        <form onSubmit={(e) => void submit(e)} className="search-form" aria-label="Propose a cell from this span">
          <div className="field">
            <label htmlFor="cite-matrix">Matrix</label>
            <select id="cite-matrix" value={chosen} onChange={(e) => setMatrixId(e.target.value)} data-testid="cite-matrix">
              {(matrices.data?.matrices ?? []).map((m) => (
                <option key={m.matrix_id} value={m.matrix_id}>
                  {m.title}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="cite-column">Column</label>
            <select
              id="cite-column"
              value={column?.id ?? ""}
              onChange={(e) => setColumnId(e.target.value)}
              data-testid="cite-column"
            >
              {columns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label} ({c.value_kind})
                </option>
              ))}
            </select>
          </div>
          {column?.value_kind === "choice" ? (
            <div className="field">
              <label htmlFor="cite-value">Value</label>
              <select id="cite-value" value={value} onChange={(e) => setValue(e.target.value)} data-testid="cite-value">
                {(column.choices ?? []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div className="field">
              <label htmlFor="cite-value">Value (as quoted)</label>
              <input id="cite-value" value={value} onChange={(e) => setValue(e.target.value)} data-testid="cite-value" />
            </div>
          )}
          {column?.value_kind === "quantity" ? (
            <>
              <div className="field">
                <label htmlFor="cite-decimal">Number (exactly as written)</label>
                <input id="cite-decimal" value={decimal} onChange={(e) => setDecimal(e.target.value)} data-testid="cite-decimal" />
              </div>
              <div className="field">
                <label htmlFor="cite-unit">Unit</label>
                <input id="cite-unit" value={unit} onChange={(e) => setUnit(e.target.value)} data-testid="cite-unit" />
              </div>
            </>
          ) : null}
          <button type="submit" className="button primary" disabled={busy || !chosen || !column} data-testid="cite-submit">
            Propose cell
          </button>
          <button type="button" className="button secondary" onClick={onClose}>
            Close
          </button>
        </form>
      )}
      {view.data && !inMatrix ? (
        <p className="inline-warning" data-testid="cite-not-in-matrix">
          This work is not in that matrix: the proposal will be refused.
        </p>
      ) : null}
      {error ? <Refusal error={error} /> : null}
      {done ? (
        <p className="muted" role="status" data-testid="cite-done">
          {done}
        </p>
      ) : null}
    </section>
  );
}
