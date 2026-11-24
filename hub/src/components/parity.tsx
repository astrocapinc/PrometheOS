// App paths for the operations that CP10.6 completed (decision 0022): notebook export, package
// import / revoke / trust, workflow export, paper requirements draft / bundle export / folder
// hand-off, recurring jobs, speech-model install and removal, the provider credential, retention
// purge and unreferenced-file collection. Each is a form over the same API an agent uses (the
// paired session is the owner), shows what it would do before it does it, and reports what it did.
import { useState, type FormEvent, type ReactNode } from "react";
import { del, describeError, get, post, q, runOperation, uuid4 } from "../api";
import { useHub, useQuery } from "../hub";
import { Details, ErrorState, KeyValues, formatDate, humanizeIds } from "./ui";

interface ProjectRoots {
  settings: { allowed_roots?: { alias: string }[] };
}

function useRoots(): string[] {
  const project = useQuery<ProjectRoots>("/project");
  return (project.data?.settings.allowed_roots ?? []).map((r) => r.alias);
}

function RootSelect({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const roots = useRoots();
  const chosen = value || roots[0] || "";
  return (
    <div className="field">
      <label htmlFor={id}>Research folder</label>
      <select id={id} value={chosen} onChange={(e) => onChange(e.target.value)}>
        {roots.length === 0 ? <option value="">none approved</option> : null}
        {roots.map((alias) => (
          <option key={alias} value={alias}>
            {alias}
          </option>
        ))}
      </select>
    </div>
  );
}

function Outcome({ error, children }: { error: unknown; children?: ReactNode }) {
  return (
    <>
      {error ? <ErrorState error={error} /> : null}
      {children}
    </>
  );
}

// ----- Notebook: export ---------------------------------------------------------------------

interface ExportPage {
  project_seq: number;
  text: string;
  next_offset: number | null;
  entries: number;
}

export function NotebookExport() {
  const { announce } = useHub();
  const [format, setFormat] = useState<"md" | "json">("md");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const download = async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      let offset = 0;
      let atSeq: number | undefined;
      let text = "";
      let entries = 0;
      for (;;) {
        const page = await get<ExportPage>(
          `/notebook/export${q({ format, offset, limit: 100000, at_seq: atSeq })}`,
        );
        atSeq = page.project_seq;
        entries = page.entries;
        text += page.text;
        if (page.next_offset === null) break;
        offset = page.next_offset;
      }
      const blob = new Blob([text], { type: format === "md" ? "text/markdown" : "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `notebook.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      const message = `Downloaded notebook.${format}: ${entries} entr${entries === 1 ? "y" : "ies"} as of project sequence ${atSeq}.`;
      setDone(message);
      announce(message);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="notebook-export-title" className="section compact" data-testid="notebook-export">
      <h2 id="notebook-export-title">Export</h2>
      <p className="muted">Save the notebook as one file. It is written by your browser; nothing is sent anywhere.</p>
      <div className="search-form">
        <div className="field">
          <label htmlFor="notebook-export-format">Format</label>
          <select id="notebook-export-format" value={format} onChange={(e) => setFormat(e.target.value as "md" | "json")}>
            <option value="md">Markdown</option>
            <option value="json">JSON</option>
          </select>
        </div>
        <button
          type="button"
          className="button secondary"
          onClick={() => void download()}
          disabled={busy}
          aria-busy={busy}
          data-testid="notebook-export-button"
        >
          {busy ? "Preparing…" : "Download notebook"}
        </button>
      </div>
      <Outcome error={error}>
        {done ? (
          <p className="success" role="status" data-testid="notebook-export-done">
            <span aria-hidden="true">✓ </span>
            {done}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

// ----- Skills: import a package, revoke or release it ------------------------------------------

interface ImportPlan {
  name: string | null;
  version: string | null;
  action: string;
  blocking: string[];
  plan_sha256: string;
  rights_resolved: boolean;
  status_after: string;
  files: { path: string }[];
  note: string;
}

export function SkillImport({ onDone }: { onDone: () => void }) {
  const { announce } = useHub();
  const [alias, setAlias] = useState("");
  const [path, setPath] = useState("");
  const [plan, setPlan] = useState<ImportPlan | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [imported, setImported] = useState<string | null>(null);
  const roots = useRoots();
  const root = alias || roots[0] || "";
  const preview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setImported(null);
    setReviewed(false);
    try {
      const next = await post<ImportPlan>("/skills/import-plan", { root_alias: root, path: path.trim() });
      setPlan(next);
      announce(`Import plan ready for ${next.name ?? "the package"}. Nothing was imported.`);
    } catch (err) {
      setPlan(null);
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const run = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      const result = await post<{ imported: boolean }>("/skills/import", {
        root_alias: root,
        path: path.trim(),
        plan_sha256: plan.plan_sha256,
      });
      const text = result.imported
        ? `${plan.name}@${plan.version} is in the catalogue as ${plan.status_after.replace(/_/g, " ")}.`
        : `${plan.name}@${plan.version} was already in the catalogue.`;
      setImported(text);
      announce(text);
      setPlan(null);
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="skill-import-title" className="section" data-testid="skill-import">
      <h2 id="skill-import-title">Import a package</h2>
      <p className="muted">
        Choose a package folder or zip inside one of your approved research folders. You see what it
        contains and its rights before anything is added; nothing in it runs.
      </p>
      <form className="search-form" onSubmit={preview} aria-label="Plan a package import">
        <RootSelect id="skill-import-root" value={root} onChange={setAlias} />
        <div className="field grow">
          <label htmlFor="skill-import-path">Path inside the folder</label>
          <input id="skill-import-path" value={path} onChange={(e) => setPath(e.target.value)} required />
        </div>
        <button type="submit" className="button secondary" disabled={busy || !path.trim()} data-testid="skill-import-plan">
          {busy ? "Planning…" : "Plan import"}
        </button>
      </form>
      <Outcome error={error}>
        {plan ? (
          <div className="install-plan" data-testid="skill-import-preview">
            <KeyValues
              rows={[
                ["Package", `${plan.name ?? "unknown"} ${plan.version ?? ""}`],
                ["Action", plan.action],
                ["Files", String(plan.files.length)],
                ["Rights", plan.rights_resolved ? "Resolved for every file" : "Not resolved: it will be quarantined"],
                ["Status after import", plan.status_after.replace(/_/g, " ")],
                ["Blocking problems", plan.blocking.length ? plan.blocking.join("; ") : "none"],
              ]}
            />
            <p className="muted">{plan.note}</p>
            <div className="field">
              <label className="check">
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(e) => setReviewed(e.target.checked)}
                  data-testid="skill-import-reviewed"
                />{" "}
                I reviewed this plan
              </label>
            </div>
            <button
              type="button"
              className="button primary"
              onClick={() => void run()}
              disabled={busy || !reviewed || plan.blocking.length > 0}
              data-testid="skill-import-commit"
            >
              Import exactly this plan
            </button>
            <Details>
              <p>
                Plan digest: <code>{plan.plan_sha256}</code>
              </p>
            </Details>
          </div>
        ) : null}
        {imported ? (
          <p className="success" role="status" data-testid="skill-imported">
            <span aria-hidden="true">✓ </span>
            {imported}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

export function SkillOwnerActions({
  skillRef,
  status,
  packageDigest,
  onDone,
}: {
  skillRef: string;
  status: string;
  packageDigest: string;
  onDone: () => void;
}) {
  const { announce } = useHub();
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const restricted = status !== "available";
  const act = async (kind: "revoke" | "trust") => {
    setBusy(true);
    setError(null);
    try {
      const body = kind === "trust" ? { package_digest: packageDigest } : {};
      const result = await post<{ status: string }>(`/skills/${encodeURIComponent(skillRef)}/${kind}`, body);
      const text =
        kind === "revoke"
          ? "The package is revoked and quarantined. Copies already installed in a project stay until you remove them."
          : `The package is released: its status is now ${result.status.replace(/_/g, " ")}.`;
      setMessage(text);
      announce(text);
      setConfirm(false);
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="skill-owner-title" className="section" data-testid="skill-owner-actions">
      <h2 id="skill-owner-title">{restricted ? "Release from quarantine" : "Revoke this package"}</h2>
      <p className="muted">
        {restricted
          ? "A quarantined or revoked package is not installed anywhere until you release it after review. Inspection cannot certify that a package is safe."
          : "Revoking adds the package to the local revocation list and quarantines it. Nothing is deleted."}
      </p>
      <div className="field">
        <label className="check">
          <input
            type="checkbox"
            checked={confirm}
            onChange={(e) => setConfirm(e.target.checked)}
            data-testid="skill-owner-confirm"
          />{" "}
          {restricted ? "I reviewed this package and accept the risk" : "I want to revoke this package"}
        </label>
      </div>
      <button
        type="button"
        className={restricted ? "button primary" : "button secondary"}
        disabled={busy || !confirm}
        onClick={() => void act(restricted ? "trust" : "revoke")}
        data-testid={restricted ? "skill-trust" : "skill-revoke"}
      >
        {restricted ? "Release package" : "Revoke package"}
      </button>
      <Outcome error={error}>
        {message ? (
          <p className="success" role="status" data-testid="skill-owner-result">
            <span aria-hidden="true">✓ </span>
            {message}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

// ----- Workflow run: export -----------------------------------------------------------------------

export function WorkflowExport({ id }: { id: string }) {
  const { announce } = useHub();
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const save = async () => {
    setError(null);
    try {
      const document_ = await get<unknown>(`/workflows/${encodeURIComponent(id)}/export`);
      const blob = new Blob([JSON.stringify(document_, null, 2) + "\n"], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `workflow-run.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setDone(true);
      announce("Downloaded the run and its recipe as JSON.");
    } catch (err) {
      setError(err);
    }
  };
  return (
    <section aria-labelledby="workflow-export-title" className="section compact">
      <h2 id="workflow-export-title">Export</h2>
      <button type="button" className="button secondary" onClick={() => void save()} data-testid="workflow-export">
        Download run as JSON
      </button>
      <Outcome error={error}>
        {done ? (
          <p className="success" role="status" data-testid="workflow-export-done">
            <span aria-hidden="true">✓ </span>The run, its recipe and its ready steps were saved.
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

// ----- Paper: requirements draft, bundle export, folder hand-off ---------------------------------

interface RequirementRow {
  text?: string;
  requirement?: string;
  [key: string]: unknown;
}

interface HandoffPlan {
  digest: string;
  entries: { path: string; action: string; role?: string }[];
  into: { root_alias: string; path: string };
}

export function PaperTools({ paperId, venueVersionIds }: { paperId: string; venueVersionIds: string[] }) {
  return (
    <>
      <RequirementsDraft venueVersionIds={venueVersionIds} />
      <BundleExport paperId={paperId} />
      <Handoff paperId={paperId} />
    </>
  );
}

function RequirementsDraft({ venueVersionIds }: { venueVersionIds: string[] }) {
  const [error, setError] = useState<unknown>(null);
  const [rows, setRows] = useState<RequirementRow[] | null>(null);
  const [version, setVersion] = useState("");
  const chosen = version || venueVersionIds[0] || "";
  const draft = async () => {
    setError(null);
    try {
      const result = await get<{ requirements: RequirementRow[] }>(
        `/versions/${encodeURIComponent(chosen)}/requirements-draft`,
      );
      setRows(result.requirements);
    } catch (err) {
      setRows(null);
      setError(err);
    }
  };
  return (
    <section aria-labelledby="req-draft-title" className="section compact" data-testid="requirements-draft">
      <h2 id="req-draft-title">Draft requirements from the venue guidance</h2>
      <p className="muted">
        Copies list items of a retained venue source into unknown rows. Nothing is saved: you decide each row's
        applicability and status yourself.
      </p>
      {venueVersionIds.length === 0 ? (
        <p className="muted">No venue guidance is recorded for this paper.</p>
      ) : (
        <>
          {venueVersionIds.length > 1 ? (
            <div className="field">
              <label htmlFor="req-draft-version">Venue source version</label>
              <select id="req-draft-version" value={chosen} onChange={(e) => setVersion(e.target.value)}>
                {venueVersionIds.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <button type="button" className="button secondary" onClick={() => void draft()} data-testid="requirements-draft-button">
            Draft rows
          </button>
        </>
      )}
      <Outcome error={error}>
        {rows ? (
          <ul className="plain-list" data-testid="requirements-draft-rows">
            {rows.map((row, i) => (
              <li key={i}>{String(row.text ?? row.requirement ?? JSON.stringify(row))}</li>
            ))}
          </ul>
        ) : null}
      </Outcome>
    </section>
  );
}

function BundleExport({ paperId }: { paperId: string }) {
  const { announce } = useHub();
  const [alias, setAlias] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<{ sha256: string; files: unknown[]; out: { root_alias: string; path: string } } | null>(null);
  const roots = useRoots();
  const root = alias || roots[0] || "";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const done = await post<NonNullable<typeof result>>(`/papers/${encodeURIComponent(paperId)}/export`, {
        root_alias: root,
        path: path.trim(),
      });
      setResult(done);
      announce(`Bundle written to ${done.out.root_alias}/${done.out.path}.`);
    } catch (err) {
      setResult(null);
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="bundle-export-title" className="section compact" data-testid="bundle-export">
      <h2 id="bundle-export-title">Export a bundle</h2>
      <p className="muted">
        Writes one new .zip bound to the exact manuscript and source versions, inside an approved research folder. It
        sends nothing and never overwrites a file.
      </p>
      <form className="search-form" onSubmit={submit} aria-label="Export the paper bundle">
        <RootSelect id="bundle-root" value={root} onChange={setAlias} />
        <div className="field grow">
          <label htmlFor="bundle-path">New file (a .zip path inside the folder)</label>
          <input id="bundle-path" value={path} onChange={(e) => setPath(e.target.value)} required />
        </div>
        <button type="submit" className="button secondary" disabled={busy || !path.trim()} data-testid="bundle-export-button">
          {busy ? "Writing…" : "Write bundle"}
        </button>
      </form>
      <Outcome error={error}>
        {result ? (
          <p className="success" role="status" data-testid="bundle-export-done">
            <span aria-hidden="true">✓ </span>
            Wrote <code>{result.out.root_alias}/{result.out.path}</code> with {result.files.length} file(s). SHA-256{" "}
            <code>{result.sha256}</code>.
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

function Handoff({ paperId }: { paperId: string }) {
  const { announce } = useHub();
  const [alias, setAlias] = useState("");
  const [folder, setFolder] = useState("");
  const [plan, setPlan] = useState<HandoffPlan | null>(null);
  const [overwrite, setOverwrite] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [written, setWritten] = useState<string | null>(null);
  const roots = useRoots();
  const root = alias || roots[0] || "";
  const compare = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setWritten(null);
    setOverwrite([]);
    try {
      setPlan(
        await get<HandoffPlan>(
          `/papers/${encodeURIComponent(paperId)}/handoff/plan${q({ root_alias: root, folder: folder.trim() })}`,
        ),
      );
    } catch (err) {
      setPlan(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      await post(`/papers/${encodeURIComponent(paperId)}/handoff/apply`, {
        root_alias: root,
        folder: folder.trim() || null,
        digest: plan.digest,
        overwrite,
      });
      const count = plan.entries.filter((e) => e.action === "new" || overwrite.includes(e.path)).length;
      const text = `Wrote ${count} file(s) into ${root}/${folder.trim()}. Nothing was deleted and Git was not run.`;
      setWritten(text);
      announce(text);
      setPlan(null);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const conflicts = plan?.entries.filter((e) => e.action === "conflict") ?? [];
  const pending = plan?.entries.filter((e) => e.action !== "identical" && e.action !== "folder_only") ?? [];
  return (
    <section aria-labelledby="handoff-title" className="section compact" data-testid="handoff">
      <h2 id="handoff-title">Hand off to a folder</h2>
      <p className="muted">
        Compares the manuscript bundle with a folder inside an approved research folder (for example your Git clone),
        then writes only the files you reviewed. It never deletes and never runs Git.
      </p>
      <form className="search-form" onSubmit={compare} aria-label="Compare the bundle with a folder">
        <RootSelect id="handoff-root" value={root} onChange={setAlias} />
        <div className="field grow">
          <label htmlFor="handoff-folder">Folder inside it</label>
          <input id="handoff-folder" value={folder} onChange={(e) => setFolder(e.target.value)} />
        </div>
        <button type="submit" className="button secondary" disabled={busy} data-testid="handoff-compare">
          {busy ? "Comparing…" : "Compare"}
        </button>
      </form>
      <Outcome error={error}>
        {plan ? (
          <div data-testid="handoff-plan">
            <table className="plain-table">
              <caption className="visually-hidden">Files the hand-off would write, and what is already there</caption>
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">What would happen</th>
                  <th scope="col">Overwrite</th>
                </tr>
              </thead>
              <tbody>
                {plan.entries.map((entry) => (
                  <tr key={entry.path} data-action={entry.action}>
                    <td>
                      <code>{entry.path}</code>
                    </td>
                    <td>{entry.action.replace(/_/g, " ")}</td>
                    <td>
                      {entry.action === "conflict" ? (
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={overwrite.includes(entry.path)}
                            onChange={(e) =>
                              setOverwrite((current) =>
                                e.target.checked ? [...current, entry.path] : current.filter((p) => p !== entry.path),
                              )
                            }
                          />{" "}
                          <span className="visually-hidden">Overwrite {entry.path}</span>
                          overwrite
                        </label>
                      ) : (
                        <span className="muted">n/a</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted">
              {pending.length} file(s) to write; {conflicts.length} conflict(s) stay untouched unless you tick them.
            </p>
            <button type="button" className="button primary" onClick={() => void apply()} disabled={busy} data-testid="handoff-apply">
              Write the reviewed files
            </button>
          </div>
        ) : null}
        {written ? (
          <p className="success" role="status" data-testid="handoff-done">
            <span aria-hidden="true">✓ </span>
            {written}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

// ----- Jobs ---------------------------------------------------------------------------------------

interface JobRow {
  job: {
    id: string;
    rev: number;
    body: { handler: string; enabled: boolean; schedule: { type: string; minutes?: number; start_utc?: string }; parameters: Record<string, unknown> };
  };
  recent_slots: { due_utc: string; state: string; reason: string | null }[];
  next_slots: { due_utc?: string }[];
}

interface JobPlanView {
  next_slots: { due_utc?: string; local?: string; skipped?: string }[];
  side_effects: string;
  transmits_data: boolean;
  runs_only_when: string;
  coalescing: string;
}

interface SlotRow {
  due_utc: string;
  state: string;
  attempts: number;
  reason: string | null;
}

export function JobsPanel({ papers }: { papers: { id: string; title: string }[] }) {
  const jobs = useQuery<{ jobs: JobRow[] }>("/jobs");
  const { announce, refresh } = useHub();
  const [handler, setHandler] = useState("digest");
  const [paper, setPaper] = useState("");
  const [minutes, setMinutes] = useState("60");
  const [plan, setPlan] = useState<JobPlanView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const body = () => ({
    handler,
    parameters: handler === "paper_check" ? { paper_id: paper || papers[0]?.id } : {},
    schedule: { type: "interval", minutes: Number(minutes), start_utc: new Date().toISOString().replace(/\.\d+Z$/, "Z") },
    enabled: true,
    budget: {
      max_inputs: 50,
      max_output_chars: 4000,
      max_provider_calls: 0,
      max_concurrent_runs: 1,
      spend_cap_estimate_usd: null,
    },
    last_slot: null,
  });
  const preview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setPlan(await post<JobPlanView>("/jobs/plan", { job: body() }));
    } catch (err) {
      setPlan(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      await post("/jobs", { job: body(), request_id: uuid4() });
      announce("Job added. It runs only when due checks run.");
      setPlan(null);
      jobs.reload();
      await refresh();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="jobs-title" className="section compact" data-testid="jobs">
      <h2 id="jobs-title">Recurring jobs</h2>
      <p className="muted">
        A job is one of a fixed set of local checks on a schedule. It runs only when due checks run; see what it
        would do first.
      </p>
      <form className="search-form" onSubmit={preview} aria-label="Plan a recurring job">
        <div className="field">
          <label htmlFor="job-handler">Check</label>
          <select id="job-handler" value={handler} onChange={(e) => setHandler(e.target.value)}>
            <option value="digest">Project digest</option>
            <option value="paper_check">Paper readiness</option>
          </select>
        </div>
        {handler === "paper_check" ? (
          <div className="field">
            <label htmlFor="job-paper">Paper</label>
            <select id="job-paper" value={paper || papers[0]?.id || ""} onChange={(e) => setPaper(e.target.value)}>
              {papers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <div className="field">
          <label htmlFor="job-minutes">Every (minutes, at least 15)</label>
          <input id="job-minutes" type="number" min={15} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
        </div>
        <button type="submit" className="button secondary" disabled={busy} data-testid="job-plan">
          Preview schedule
        </button>
      </form>
      <Outcome error={error}>
        {plan ? (
          <div className="install-plan" data-testid="job-plan-view">
            <p>
              Next due: {plan.next_slots.slice(0, 3).map((s) => formatDate(s.due_utc ?? null)).join("; ") || "none"}
            </p>
            <p>{plan.side_effects}</p>
            <p className="muted">
              {plan.runs_only_when}. {plan.coalescing}
            </p>
            <button type="button" className="button primary" onClick={() => void add()} disabled={busy} data-testid="job-add">
              Add this job
            </button>
          </div>
        ) : null}
      </Outcome>
      {(jobs.data?.jobs ?? []).length === 0 ? (
        <p className="muted">No recurring jobs.</p>
      ) : (
        <ul className="card-list" data-testid="job-list">
          {(jobs.data?.jobs ?? []).map((row) => (
            <JobCard key={row.job.id} row={row} onChange={() => jobs.reload()} />
          ))}
        </ul>
      )}
    </section>
  );
}

function JobCard({ row, onChange }: { row: JobRow; onChange: () => void }) {
  const { announce } = useHub();
  const [slots, setSlots] = useState<SlotRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const toggle = async () => {
    setError(null);
    try {
      await post(`/jobs/${encodeURIComponent(row.job.id)}/enabled`, {
        base_rev: row.job.rev,
        enabled: !row.job.body.enabled,
        request_id: uuid4(),
      });
      announce(row.job.body.enabled ? "Job disabled." : "Job enabled.");
      onChange();
    } catch (err) {
      setError(err);
    }
  };
  const showSlots = async () => {
    setError(null);
    try {
      setSlots((await get<{ slots: SlotRow[] }>(`/jobs/${encodeURIComponent(row.job.id)}/slots`)).slots);
    } catch (err) {
      setError(err);
    }
  };
  const cancel = async (due: string) => {
    setError(null);
    try {
      await post(`/jobs/${encodeURIComponent(row.job.id)}/cancel`, { slot: due });
      announce("Slot cancelled.");
      await showSlots();
      onChange();
    } catch (err) {
      setError(err);
    }
  };
  const { body } = row.job;
  return (
    <li className="card" data-testid="job-card" data-job-id={row.job.id} data-enabled={String(body.enabled)}>
      <div className="card-head">
        <span className="card-title">{humanizeIds(body.handler.replace(/_/g, " "))}</span>
        <span className="badge badge-category">{body.enabled ? "Enabled" : "Disabled"}</span>
      </div>
      <p className="card-meta">
        Every {body.schedule.minutes ?? "?"} minutes · next{" "}
        {row.next_slots[0]?.due_utc ? formatDate(row.next_slots[0].due_utc) : "none"}
      </p>
      <div className="button-row">
        <button type="button" className="button secondary small" onClick={() => void toggle()} data-testid="job-toggle">
          {body.enabled ? "Disable" : "Enable"}
        </button>
        <button type="button" className="button secondary small" onClick={() => void showSlots()} data-testid="job-slots">
          Show slots
        </button>
      </div>
      <Outcome error={error}>
        {slots ? (
          slots.length === 0 ? (
            <p className="muted" data-testid="job-slot-list">
              No slot has been recorded yet.
            </p>
          ) : (
            <table className="plain-table" data-testid="job-slot-list">
              <caption className="visually-hidden">Recorded slots of this job</caption>
              <thead>
                <tr>
                  <th scope="col">Due</th>
                  <th scope="col">State</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {slots.map((s) => (
                  <tr key={s.due_utc}>
                    <td>{formatDate(s.due_utc)}</td>
                    <td>{s.state}</td>
                    <td>
                      {s.state === "pending" || s.state === "running" ? (
                        <button type="button" className="link-button" onClick={() => void cancel(s.due_utc)}>
                          Cancel
                        </button>
                      ) : (
                        <span className="muted">n/a</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : null}
      </Outcome>
    </li>
  );
}

// ----- Speech model: install and remove ---------------------------------------------------------

export function ModelActions({
  plan,
  installed,
  onDone,
}: {
  plan: { model: string; title: string; total_mib: number; license: string; plan_sha256: string };
  installed: boolean;
  onDone: () => void;
}) {
  const { announce } = useHub();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);
  const act = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      if (installed) {
        await post("/audio/models/remove", { model: plan.model });
        setMessage(`${plan.title} was removed from this computer.`);
      } else {
        await runOperation("/audio/models/install", { plan_sha256: plan.plan_sha256, model: plan.model });
        setMessage(`${plan.title} is installed and verified.`);
      }
      setConfirm(false);
      announce("Done.");
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div data-testid="model-actions">
      <p className="muted small-print">
        {installed
          ? "Removing deletes the model files from this computer; recordings and transcripts are kept."
          : `Installing downloads ${plan.total_mib} MiB (licence ${plan.license}) once, verifies every file and stores it on this computer only.`}
      </p>
      <label className="check">
        <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} data-testid="model-confirm" />{" "}
        {installed ? "I want to remove this model" : "I reviewed the plan above and want to download it"}
      </label>
      <div className="button-row">
        <button
          type="button"
          className={installed ? "button secondary" : "button primary"}
          disabled={busy || !confirm}
          aria-busy={busy}
          onClick={() => void act()}
          data-testid={installed ? "model-remove" : "model-install"}
        >
          {busy ? "Working…" : installed ? "Remove model" : "Download and install"}
        </button>
      </div>
      <Outcome error={error}>
        {message ? (
          <p className="success" role="status" data-testid="model-result">
            <span aria-hidden="true">✓ </span>
            {message}
          </p>
        ) : null}
      </Outcome>
    </div>
  );
}

// ----- Provider: the credential --------------------------------------------------------------------

export function CredentialDelete({ reference, onDone }: { reference: string | null; onDone: () => void }) {
  const { announce } = useHub();
  const [name, setName] = useState(reference ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);
  const remove = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await del(`/provider/secrets${q({ reference: name.trim() })}`);
      setMessage(`The stored value for ${name.trim()} was deleted.`);
      announce("Credential deleted.");
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div aria-label="Delete a credential" data-testid="credential-delete-panel">
      <p className="muted small-print">
        Credential entry uses the terminal's hidden prompt. You can delete a stored value here.
      </p>
      <div className="field">
        <label htmlFor="credential-reference">Credential name</label>
        <input id="credential-reference" value={name} onChange={(e) => setName(e.target.value)} required />
      </div>
      <div className="button-row">
        <button type="button" className="button secondary" disabled={busy || !name.trim()} onClick={() => void remove()} data-testid="credential-delete">
          Delete stored value
        </button>
      </div>
      <Outcome error={error}>
        {message ? (
          <p className="success" role="status" data-testid="credential-result">
            <span aria-hidden="true">✓ </span>
            {message}
          </p>
        ) : null}
      </Outcome>
    </div>
  );
}

// ----- Retention: plan and commit a purge ---------------------------------------------------------

interface PurgePlan {
  plan_id: string;
  plan_sha256: string;
  targets: string[];
  closure: string[];
  payloads: string[];
  media: string[];
  limitations: string[];
}

export function RetentionPanel() {
  const { announce, refresh } = useHub();
  const [ids, setIds] = useState("");
  const [plan, setPlan] = useState<PurgePlan | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);
  const preview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    setConfirm(false);
    try {
      setPlan(
        await post<PurgePlan>("/retention/purge/plan", {
          ids: ids.split(/[\s,]+/).filter((x) => x.length > 0),
        }),
      );
    } catch (err) {
      setPlan(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const commit = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      await post(`/retention/purge/${encodeURIComponent(plan.plan_id)}/commit`, {
        plan_sha256: plan.plan_sha256,
      });
      const text = `Purged ${plan.closure.length} record(s). The content is gone; the deletion is recorded without it.`;
      setMessage(text);
      announce(text);
      setPlan(null);
      setIds("");
      await refresh();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="retention-title" className="section" data-testid="retention">
      <h2 id="retention-title">Retention: purge records</h2>
      <p className="muted">
        Permanently removes records and everything that depends on them. You see the full list and its limits first, and
        an agent can only ask: it never purges on its own.
      </p>
      <form className="search-form" onSubmit={preview} aria-label="Plan a purge">
        <div className="field grow">
          <label htmlFor="retention-ids">Record IDs to purge (separated by spaces or commas)</label>
          <input id="retention-ids" value={ids} onChange={(e) => setIds(e.target.value)} required />
        </div>
        <button type="submit" className="button secondary" disabled={busy || !ids.trim()} data-testid="retention-plan">
          Plan purge
        </button>
      </form>
      <Outcome error={error}>
        {plan ? (
          <div className="install-plan" data-testid="retention-preview">
            <p>
              This would purge {plan.closure.length} record(s), {plan.payloads.length} stored payload(s) and{" "}
              {plan.media.length} media file(s):
            </p>
            <ul className="plain-list">
              {plan.closure.map((id) => (
                <li key={id}>
                  <code>{id}</code>
                </li>
              ))}
            </ul>
            <ul className="plain-list">
              {plan.limitations.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <label className="check">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} data-testid="retention-confirm" />{" "}
              I understand this cannot be undone
            </label>
            <div className="button-row">
              <button type="button" className="button primary" onClick={() => void commit()} disabled={busy || !confirm} data-testid="retention-commit">
                Purge exactly this plan
              </button>
            </div>
            <Details>
              <p>
                Plan digest: <code>{plan.plan_sha256}</code>
              </p>
            </Details>
          </div>
        ) : null}
        {message ? (
          <p className="success" role="status" data-testid="retention-done">
            <span aria-hidden="true">✓ </span>
            {message}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

// ----- Artifacts: unreferenced files --------------------------------------------------------------

export function OrphansPanel() {
  const { announce } = useHub();
  const [report, setReport] = useState<{ orphans: unknown[]; collected?: unknown[] } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const run = async (collect: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const result = await post<{ orphans: unknown[]; collected?: unknown[] }>("/maintenance/orphans", { collect });
      setReport(result);
      announce(collect ? "Unreferenced files collected." : "Checked for unreferenced files.");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="orphans-title" className="section compact" data-testid="orphans">
      <h2 id="orphans-title">Unreferenced files</h2>
      <p className="muted">
        Media and staging files no record refers to, older than a day. Checking changes nothing; collecting deletes only
        those files.
      </p>
      <div className="button-row">
        <button type="button" className="button secondary" onClick={() => void run(false)} disabled={busy} data-testid="orphans-check">
          Check
        </button>
        {report && report.orphans.length > 0 ? (
          <button type="button" className="button secondary" onClick={() => void run(true)} disabled={busy} data-testid="orphans-collect">
            Collect {report.orphans.length} file(s)
          </button>
        ) : null}
      </div>
      <Outcome error={error}>
        {report ? (
          <p role="status" data-testid="orphans-result">
            {report.orphans.length === 0
              ? "No unreferenced file is past its grace period."
              : `${report.orphans.length} unreferenced file(s) past their grace period.`}
          </p>
        ) : null}
      </Outcome>
    </section>
  );
}

