// Building an evidence matrix in the hub (A02, CP11 S11.4b; decision 0066 §2): the creation form
// (title, an optional question, columns with their kind, instructions and choices, and the works
// picked from the library) and the per-column deterministic rules (keywords, or the bounded
// pattern language with one {value}), run as an operation run whose counts and reasons are
// shown. Every button calls a registry operation; the server checks everything.
import { useState, type FormEvent } from "react";
import { ApiError, describeError, post, runOperation } from "../api";
import { ErrorState, QueryView } from "../components/ui";
import { useHub, useQuery } from "../hub";
import { Link } from "../router";
import type { LibraryWorks, MatrixColumn, MatrixView } from "../types";

interface ColumnDraft {
  label: string;
  kind: "text" | "quantity" | "choice";
  instructions: string;
  choices: string;
}

const EMPTY: ColumnDraft = { label: "", kind: "text", instructions: "", choices: "" };

/** A column ID from its label (a-z first, then a-z, 0-9, '-'; unique in the form). */
function columnIds(drafts: ColumnDraft[]): string[] {
  const seen = new Set<string>();
  return drafts.map((d, i) => {
    let base = d.label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^[^a-z]+/, "").replace(/-+$/, "");
    base = (base || `column-${i + 1}`).slice(0, 36);
    let id = base;
    for (let n = 2; seen.has(id); n += 1) id = `${base}-${n}`;
    seen.add(id);
    return id;
  });
}

function Refusal({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.kind === "validation") {
    return (
      <p className="inline-warning" role="alert" data-testid="matrix-form-refusal">
        <strong>Not accepted:</strong> {error.message}
      </p>
    );
  }
  return <ErrorState error={error} />;
}

export function CreateMatrix({ onCreated }: { onCreated: (matrixId: string) => void }) {
  const { announce } = useHub();
  const works = useQuery<LibraryWorks>("/library/works?limit=200");
  const questions = useQuery<{ items: { id: string; body: { title: string; category: string } }[] }>(
    "/records?kind=entry&limit=200",
  );
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [columns, setColumns] = useState<ColumnDraft[]>([{ ...EMPTY }]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const update = (i: number, patch: Partial<ColumnDraft>) =>
    setColumns((all) => all.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const ids = columnIds(columns);
      const made = await post<{ matrix_id: string }>("/matrix", {
        title: title.trim(),
        columns: columns.map((c, i) => ({
          id: ids[i],
          label: c.label.trim(),
          value_kind: c.kind,
          instructions: c.instructions.trim(),
          ...(c.kind === "choice"
            ? { choices: c.choices.split(",").map((v) => v.trim()).filter(Boolean) }
            : {}),
        })),
        work_ids: [...picked],
        ...(question ? { question_id: question } : {}),
      });
      announce("Matrix created.");
      setTitle("");
      setColumns([{ ...EMPTY }]);
      setPicked(new Set());
      onCreated(made.matrix_id);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const questionEntries = (questions.data?.items ?? []).filter((e) => e.body.category === "question");
  return (
    <details className="card" data-testid="matrix-create">
      <summary>
        <strong>New matrix</strong>: columns to fill for each chosen work
      </summary>
      <form onSubmit={(e) => void submit(e)} aria-label="Create a matrix">
        <div className="field">
          <label htmlFor="matrix-title">Title</label>
          <input id="matrix-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} data-testid="matrix-create-title" />
        </div>
        <div className="field">
          <label htmlFor="matrix-question">Question (optional)</label>
          <select id="matrix-question" value={question} onChange={(e) => setQuestion(e.target.value)} data-testid="matrix-create-question">
            <option value="">No question</option>
            {questionEntries.map((e) => (
              <option key={e.id} value={e.id}>
                {e.body.title}
              </option>
            ))}
          </select>
        </div>
        <fieldset>
          <legend>Columns</legend>
          {columns.map((c, i) => (
            <div key={i} className="matrix-column-draft" data-testid={`matrix-create-column-${i}`}>
              <div className="field">
                <label htmlFor={`col-label-${i}`}>Label</label>
                <input id={`col-label-${i}`} value={c.label} onChange={(e) => update(i, { label: e.target.value })} data-testid={`matrix-create-label-${i}`} />
              </div>
              <div className="field">
                <label htmlFor={`col-kind-${i}`}>Kind</label>
                <select
                  id={`col-kind-${i}`}
                  value={c.kind}
                  onChange={(e) => update(i, { kind: e.target.value as ColumnDraft["kind"] })}
                  data-testid={`matrix-create-kind-${i}`}
                >
                  <option value="text">Text</option>
                  <option value="quantity">Quantity (number and unit)</option>
                  <option value="choice">Choice</option>
                </select>
              </div>
              {c.kind === "choice" ? (
                <div className="field">
                  <label htmlFor={`col-choices-${i}`}>Choices (comma-separated)</label>
                  <input id={`col-choices-${i}`} value={c.choices} onChange={(e) => update(i, { choices: e.target.value })} data-testid={`matrix-create-choices-${i}`} />
                </div>
              ) : null}
              <div className="field grow">
                <label htmlFor={`col-instr-${i}`}>Instructions</label>
                <input id={`col-instr-${i}`} value={c.instructions} onChange={(e) => update(i, { instructions: e.target.value })} />
              </div>
              {columns.length > 1 ? (
                <button type="button" className="button secondary small" onClick={() => setColumns((all) => all.filter((_, j) => j !== i))}>
                  Remove column
                </button>
              ) : null}
            </div>
          ))}
          <button type="button" className="button secondary small" onClick={() => setColumns((all) => [...all, { ...EMPTY }])} data-testid="matrix-create-add-column">
            Add column
          </button>
        </fieldset>
        <fieldset>
          <legend>Works</legend>
          <QueryView query={works} loadingLabel="Loading the library" isEmpty={(d) => d.works.length === 0} empty={<>No works yet: import papers in the <Link to="/knowledge/library">library</Link>.</>}>
            {(data) => (
              <ul className="plain-list" data-testid="matrix-create-works">
                {data.works.map((w) => (
                  <li key={w.work_id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={picked.has(w.work_id)}
                        onChange={(e) =>
                          setPicked((all) => {
                            const next = new Set(all);
                            if (e.target.checked) next.add(w.work_id);
                            else next.delete(w.work_id);
                            return next;
                          })
                        }
                        data-testid={`matrix-create-work-${w.work_id}`}
                      />{" "}
                      {w.title} <span className="muted">({w.access_state.replace("_", " ")})</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </QueryView>
        </fieldset>
        <button type="submit" className="button primary" disabled={busy || !title.trim()} data-testid="matrix-create-submit">
          Create matrix
        </button>
      </form>
      {error ? <Refusal error={error} /> : null}
    </details>
  );
}

interface RuleDraft {
  mode: "keywords" | "pattern";
  text: string;
  unit: string;
  value: string;
}

interface RuleRow {
  work_id: string;
  extraction_id: string | null;
  action: string;
  cells: Record<string, string>;
  reason: string | null;
  refused_cells: Record<string, string>;
}

interface RuleResult {
  run_id: string;
  counts: Record<string, number>;
  rows: RuleRow[];
}

function draftFor(column: MatrixColumn): RuleDraft {
  return {
    mode: column.value_kind === "quantity" ? "pattern" : "keywords",
    text: "",
    unit: "",
    value: column.choices?.[0] ?? "",
  };
}

export function RulesPanel({ view, onRan }: { view: MatrixView; onRan: () => void }) {
  const { announce } = useHub();
  const columns = view.matrix.columns;
  const [drafts, setDrafts] = useState<Record<string, RuleDraft>>(() =>
    Object.fromEntries(columns.map((c) => [c.id, draftFor(c)])),
  );
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RuleResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const titles = Object.fromEntries(view.rows.map((r) => [r.work_id, r.work_title]));
  const update = (id: string, patch: Partial<RuleDraft>) =>
    setDrafts((all) => ({ ...all, [id]: { ...(all[id] ?? draftFor(columns.find((c) => c.id === id)!)), ...patch } }));
  const run = async (event: FormEvent) => {
    event.preventDefault();
    const rules = columns.flatMap((c) => {
      const d = drafts[c.id];
      if (!d || !d.text.trim()) return [];
      const rule: Record<string, unknown> = { id: `${c.id}-1`.slice(0, 40), column: c.id };
      if (d.mode === "keywords") rule.keywords = d.text.split("\n").map((k) => k.trim()).filter(Boolean);
      else rule.pattern = d.text.trim();
      if (c.value_kind === "quantity" && d.unit.trim()) rule.unit = d.unit.trim();
      if (c.value_kind === "choice") rule.value = d.value;
      return [rule];
    });
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const done = await runOperation<RuleResult>(`/matrix/${encodeURIComponent(view.matrix.matrix_id)}/extract/run`, { rules: { rules } });
      setResult(done);
      announce("Rules ran.");
      onRan();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setRunning(false);
    }
  };
  const stateCounts: Record<string, number> = {};
  for (const row of result?.rows ?? []) for (const s of Object.values(row.cells)) stateCounts[s] = (stateCounts[s] ?? 0) + 1;
  return (
    <details className="card" data-testid="matrix-rules">
      <summary>
        <strong>Rules</strong>: fill columns from the papers' text with keywords or a pattern
      </summary>
      <p className="muted">
        Keywords: one per line; a hit proposes the cell (for a choice column, the value you pick here; for a
        text column, the matched words). Pattern: literal words with at most one {"{value}"} for a number; a
        quantity column needs a pattern and its unit. No match makes the cell unsupported. Reviewed cells and
        cells you proposed are never replaced.
      </p>
      <form onSubmit={(e) => void run(e)} aria-label="Run rules">
        {columns.map((c) => {
          const d = drafts[c.id] ?? draftFor(c);
          return (
            <fieldset key={c.id} data-testid={`matrix-rule-${c.id}`}>
              <legend>
                {c.label} <span className="muted">({c.value_kind})</span>
              </legend>
              {c.value_kind !== "quantity" ? (
                <div className="field">
                  <label htmlFor={`rule-mode-${c.id}`}>Kind of rule</label>
                  <select id={`rule-mode-${c.id}`} value={d.mode} onChange={(e) => update(c.id, { mode: e.target.value as RuleDraft["mode"] })} data-testid={`matrix-rule-mode-${c.id}`}>
                    <option value="keywords">Keywords</option>
                    <option value="pattern">Pattern</option>
                  </select>
                </div>
              ) : null}
              <div className="field grow">
                <label htmlFor={`rule-text-${c.id}`}>{d.mode === "keywords" ? "Keywords (one per line)" : "Pattern"}</label>
                {d.mode === "keywords" ? (
                  <textarea id={`rule-text-${c.id}`} rows={2} value={d.text} onChange={(e) => update(c.id, { text: e.target.value })} data-testid={`matrix-rule-text-${c.id}`} />
                ) : (
                  <input id={`rule-text-${c.id}`} value={d.text} onChange={(e) => update(c.id, { text: e.target.value })} placeholder="evacuated to {value}" data-testid={`matrix-rule-text-${c.id}`} />
                )}
              </div>
              {c.value_kind === "quantity" ? (
                <div className="field">
                  <label htmlFor={`rule-unit-${c.id}`}>Unit</label>
                  <input id={`rule-unit-${c.id}`} value={d.unit} onChange={(e) => update(c.id, { unit: e.target.value })} data-testid={`matrix-rule-unit-${c.id}`} />
                </div>
              ) : null}
              {c.value_kind === "choice" ? (
                <div className="field">
                  <label htmlFor={`rule-value-${c.id}`}>Value on a hit</label>
                  <select id={`rule-value-${c.id}`} value={d.value} onChange={(e) => update(c.id, { value: e.target.value })} data-testid={`matrix-rule-value-${c.id}`}>
                    {(c.choices ?? []).map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
            </fieldset>
          );
        })}
        <button type="submit" className="button primary" disabled={running} data-testid="matrix-rules-run">
          Run rules
        </button>
      </form>
      {running ? (
        <p className="muted" role="status" data-testid="matrix-rules-running">
          Running the rules (an operation run; see Runs)…
        </p>
      ) : null}
      {error ? <Refusal error={error} /> : null}
      {result ? (
        <div data-testid="matrix-rules-result" role="status">
          <p>
            Run <Link to={`/runs/${result.run_id}`}>{result.run_id}</Link>: rows{" "}
            {Object.entries(result.counts)
              .map(([k, v]) => `${k} ${v}`)
              .join(", ")}
            ; cells {Object.entries(stateCounts).map(([k, v]) => `${k} ${v}`).join(", ") || "none"}.
          </p>
          <ul className="plain-list">
            {result.rows
              .filter((r) => r.reason || Object.keys(r.refused_cells).length > 0)
              .map((r) => (
                <li key={r.work_id} data-testid="matrix-rules-reason">
                  {titles[r.work_id] ?? r.work_id}: {r.reason ?? ""}
                  {Object.entries(r.refused_cells).map(([col, why]) => (
                    <span key={col}>
                      {" "}
                      {col} refused: {why}
                    </span>
                  ))}
                </li>
              ))}
          </ul>
        </div>
      ) : null}
    </details>
  );
}
