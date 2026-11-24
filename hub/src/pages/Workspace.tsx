// A paper's LaTeX workspace (A01, CP9.3): file tree and outline, a CodeMirror editor with
// completion, find/replace and an anchor gutter, compile with diagnostics, a PDF.js preview
// with SyncTeX both ways, evidence insertion and the Overleaf Git panel.
//
// Files on disk are the manuscript. Saving names the digest the editor loaded; a file that
// changed on disk is never overwritten: the save is refused and the draft stays here until
// the researcher decides. Unsaved drafts live only in this page's memory.
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, describeError, get, post, put, q, runOperation } from "../api";
import { PdfView, type Highlight } from "../components/PdfView";
import { TexEditor, type EditorHandle, type GutterMark } from "../components/TexEditor";
import { ErrorState, Loading, PageHeading, formatDate } from "../components/ui";
import { useKindList } from "../data";
import { useQuery } from "../hub";
import { Link } from "../router";
import type { SourceBody, VersionBody } from "../types";
import type {
  CompileResult,
  CompileStatus,
  Diagnostic,
  EngineInfo,
  FileContent,
  ForwardResult,
  GutterAnchor,
  InsertResult,
  InverseResult,
  OverleafPreview,
  OverleafStatus,
  Structure,
  WorkspaceFiles,
} from "../workspaceTypes";

interface OpenFile {
  path: string;
  base: string; // digest of the bytes the editor loaded (or last saved)
  loaded: string; // text as loaded
  text: string; // current buffer
  crlf: boolean;
  loadId: number;
  line?: number; // where to put the cursor when this load opens
}

interface Conflict {
  path: string;
  diskSha: string | null;
}

const STATE_TEXT: Record<string, string> = {
  versioned: "recorded",
  changed: "changed since last version",
  unversioned: "not yet recorded",
};

function splitLines(text: string): string[] {
  return text.split("\n").map((l) => (l.endsWith("\r") ? l.slice(0, -1) : l));
}

/** Locate an anchor's exact lines in the current text (the remap rule: unique match only). */
export function locateAnchor(anchor: GutterAnchor, lines: string[]): number | null {
  const wanted = anchor.anchored_lines;
  if (!wanted || wanted.length === 0) return null;
  let found: number | null = null;
  for (let i = 0; i + wanted.length <= lines.length; i += 1) {
    let same = true;
    for (let j = 0; j < wanted.length; j += 1) {
      if (lines[i + j] !== wanted[j]) {
        same = false;
        break;
      }
    }
    if (same) {
      if (found !== null) return null;
      found = i + 1;
    }
  }
  return found;
}

function anchorLabel(anchor: GutterAnchor): string {
  const claims = anchor.claims.map((c) => c.statement ?? "a claim").join("; ");
  const state = anchor.status === "current" ? "" : ` (${anchor.status.replace("_", " ")})`;
  return `Claim${anchor.claims.length === 1 ? "" : "s"}${state}: ${claims || "none"}`;
}

export function WorkspacePage({ id }: { id: string }) {
  const base = `/papers/${encodeURIComponent(id)}`;
  const files = useQuery<WorkspaceFiles>(`${base}/workspace/files`);
  const structure = useQuery<Structure>(`${base}/workspace/structure`);
  const compileStatus = useQuery<CompileStatus>(`${base}/compile`);
  const engine = useQuery<EngineInfo>("/tex/engine");
  const editor = useRef<EditorHandle>(null);
  const drafts = useRef(new Map<string, OpenFile>());
  const [file, setFile] = useState<OpenFile | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [lastCompile, setLastCompile] = useState<CompileResult | null>(null);
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [sideTab, setSideTab] = useState<"files" | "outline" | "claims">("files");
  const loadCounter = useRef(0);
  const workspace = files.data?.workspace ?? structure.data?.workspace;
  const main = workspace?.main ?? null;
  const dirty = file !== null && file.text !== file.loaded;

  const openPath = useCallback(
    async (path: string, line?: number) => {
      if (file && file.path === path) {
        if (line) editor.current?.goTo(line);
        return;
      }
      if (file && file.text !== file.loaded) drafts.current.set(file.path, file);
      setError(null);
      const kept = drafts.current.get(path);
      if (kept) {
        setFile({ ...kept, loadId: ++loadCounter.current, line });
      } else {
        try {
          const content = await get<FileContent>(`${base}/workspace/file${q({ path })}`);
          if (content.text === null) {
            setNotice(`${path} is not a text file; it is not opened in the editor.`);
            return;
          }
          setFile({
            path: content.path,
            base: content.sha256,
            loaded: content.text,
            text: content.text,
            crlf: content.line_ending === "crlf",
            loadId: ++loadCounter.current,
            line,
          });
        } catch (exc) {
          setError(exc);
          return;
        }
      }
      setConflict(null);
    },
    [base, file],
  );

  // Open the main file first.
  useEffect(() => {
    if (main && file === null) void openPath(main);
  }, [main, file, openPath]);

  // Leaving with unsaved drafts asks the browser to confirm.
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty || drafts.current.size > 0) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // Watch the open file on disk: another editor, Git or an agent may change it.
  const fileRef = useRef(file);
  fileRef.current = file;
  useEffect(() => {
    if (!file) return;
    const timer = window.setInterval(() => {
      const current = fileRef.current;
      if (!current || busy) return;
      get<FileContent>(`${base}/workspace/file${q({ path: current.path })}`)
        .then((disk) => {
          const now = fileRef.current;
          if (!now || now.path !== disk.path || disk.sha256 === now.base) return;
          if (now.text === now.loaded && disk.text !== null) {
            setFile({
              ...now,
              base: disk.sha256,
              loaded: disk.text,
              text: disk.text,
              crlf: disk.line_ending === "crlf",
              loadId: ++loadCounter.current,
            });
            setNotice(`${disk.path} changed on disk and was reloaded (you had no unsaved edits).`);
          } else {
            setConflict({ path: disk.path, diskSha: disk.sha256 });
          }
        })
        .catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [base, file?.path, busy]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useCallback(
    async (overrideBase?: string | null) => {
      const current = fileRef.current;
      if (!current) return;
      setBusy("save");
      setError(null);
      const text = editor.current?.text() ?? current.text;
      try {
        const saved = await put<{ sha256: string }>(`${base}/workspace/file`, {
          path: current.path,
          text,
          base_sha256: overrideBase === undefined ? current.base : overrideBase,
        });
        drafts.current.delete(current.path);
        setFile({ ...current, base: saved.sha256, loaded: text, text });
        setConflict(null);
        setNotice(`Saved ${current.path}.`);
        files.reload();
        structure.reload();
      } catch (exc) {
        if (exc instanceof ApiError && exc.kind === "stale") {
          const disk = exc.details.disk_sha256;
          setConflict({ path: current.path, diskSha: typeof disk === "string" ? disk : null });
        } else {
          setError(exc);
        }
      } finally {
        setBusy(null);
      }
    },
    [base, files, structure],
  );

  const reloadFromDisk = async () => {
    if (!file) return;
    drafts.current.delete(file.path);
    const path = file.path;
    setFile(null);
    setConflict(null);
    const content = await get<FileContent>(`${base}/workspace/file${q({ path })}`);
    setFile({
      path,
      base: content.sha256,
      loaded: content.text ?? "",
      text: content.text ?? "",
      crlf: content.line_ending === "crlf",
      loadId: ++loadCounter.current,
    });
  };

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (exc) {
      setError(exc);
    } finally {
      setBusy(null);
    }
  };

  const compile = (fetchPackages: boolean) =>
    run("compile", async () => {
      if (dirty) setNotice("Unsaved edits are not compiled: the compile uses the files on disk.");
      const result = await runOperation<CompileResult>(`${base}/compile`, { fetch: fetchPackages });
      setLastCompile(result);
      setHighlight(null);
      compileStatus.reload();
      files.reload();
      structure.reload();
    });

  const snapshot = () =>
    run("snapshot", async () => {
      const result = await post<{ recorded: number }>(`${base}/workspace/snapshot`, {});
      setNotice(
        result.recorded === 0
          ? "Nothing changed since the last recorded versions."
          : `Recorded ${result.recorded} new file version${result.recorded === 1 ? "" : "s"}.`,
      );
      files.reload();
      structure.reload();
    });

  const forward = useCallback(async () => {
    const current = fileRef.current;
    if (!current) return;
    try {
      const line = editor.current?.cursorLine() ?? 1;
      const result = await get<ForwardResult>(
        `${base}/synctex/forward${q({ file: current.path, line })}`,
      );
      if (result.target) setHighlight({ page: result.target.page, rects: result.target.rects });
      else setNotice("This line has no position in the compiled PDF.");
    } catch (exc) {
      setError(exc);
    }
  }, [base]);

  const inverse = async (page: number, x: number, y: number) => {
    try {
      const result = await get<InverseResult>(`${base}/synctex/inverse${q({ page, x, y })}`);
      if (result.source) await openPath(result.source.file, result.source.line);
      else setNotice("No source line matches that position.");
    } catch (exc) {
      setError(exc);
    }
  };

  const lines = useMemo(() => (file ? splitLines(file.text) : []), [file]);
  const anchors = structure.data?.anchors ?? [];
  const located = useMemo(
    () =>
      anchors.map((a) => ({
        anchor: a,
        line: file && file.path === main ? locateAnchor(a, lines) : null,
      })),
    [anchors, file, lines, main],
  );
  const marks: GutterMark[] = useMemo(
    () =>
      located
        .filter((l) => l.line !== null)
        .map((l) => ({ line: l.line as number, label: anchorLabel(l.anchor), status: l.anchor.status })),
    [located],
  );
  const completion = useMemo(
    () => ({
      labels: (structure.data?.labels ?? []).map((l) => l.key),
      cites: (structure.data?.bib_keys ?? []).map((b) => ({ key: b.key, detail: b.type ?? "entry" })),
    }),
    [structure.data],
  );

  const status = compileStatus.data;
  const diagnostics: Diagnostic[] = lastCompile?.diagnostics ?? status?.last_compile?.diagnostics ?? [];
  const compileState = lastCompile?.status ?? status?.last_compile?.status ?? null;
  const pdf = status?.pdf ?? null;

  if (files.error && !files.data) {
    return (
      <section>
        <PageHeading title="LaTeX workspace" />
        <ErrorState error={files.error} onRetry={files.reload} />
        <p>
          <Link to={`/papers/${id}`}>Back to the paper</Link>
        </p>
      </section>
    );
  }
  if (!workspace) return <Loading label="Opening the workspace" />;

  return (
    <section className="tex-workspace" aria-labelledby="page-title">
      <PageHeading
        title={workspace.title}
        lead={
          <>
            LaTeX workspace in <code>{workspace.root_alias}/{workspace.folder || "."}</code>, main
            file <code>{workspace.main}</code>. <Link to={`/papers/${id}`}>Paper overview</Link>
          </>
        }
      />
      <EnginePanel engine={engine.data} reload={engine.reload} />
      <div className="tex-toolbar" role="toolbar" aria-label="Workspace actions">
        <button type="button" className="button primary small" disabled={!file || busy !== null} onClick={() => void save()}>
          Save
        </button>
        <button type="button" className="button secondary small" disabled={busy !== null} onClick={() => void compile(false)}>
          {busy === "compile" ? "Compiling…" : "Compile"}
        </button>
        <button type="button" className="button secondary small" disabled={busy !== null} onClick={() => void compile(true)}>
          Compile and fetch missing packages
        </button>
        <button type="button" className="button secondary small" disabled={!file || !pdf} onClick={() => void forward()}>
          Show line in PDF
        </button>
        <button type="button" className="button secondary small" disabled={!file} onClick={() => editor.current?.openSearch()}>
          Find and replace
        </button>
        <button type="button" className="button secondary small" disabled={busy !== null} onClick={() => void snapshot()}>
          Record versions
        </button>
        {file ? (
          <span className={dirty ? "tex-dirty" : "tex-clean"} data-testid="save-state" role="status">
            {dirty ? `Unsaved changes in ${file.path}` : `${file.path} saved`}
          </span>
        ) : null}
      </div>
      {notice ? (
        <p className="info" role="status" data-testid="workspace-notice">
          {notice}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      {conflict && file && conflict.path === file.path ? (
        <div className="inline-warning" role="alert" data-testid="external-change">
          <p>
            <strong>{conflict.path} changed on disk</strong> since you opened it. Nothing was
            written and your draft is kept in the editor.
          </p>
          <div className="button-row">
            <button type="button" className="button secondary small" onClick={() => void reloadFromDisk()}>
              Load the disk version (discard my draft)
            </button>
            <button
              type="button"
              className="button secondary small"
              onClick={() => void save(conflict.diskSha)}
            >
              Replace the disk version with my draft
            </button>
          </div>
        </div>
      ) : null}
      <div className="tex-grid">
        <aside className="tex-side" aria-label="Workspace navigation">
          <div className="tex-tabs" role="tablist" aria-label="Workspace panels">
            {(["files", "outline", "claims"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                id={`tex-tab-${tab}`}
                aria-selected={sideTab === tab}
                aria-controls={`tex-panel-${tab}`}
                className={sideTab === tab ? "tex-tab selected" : "tex-tab"}
                onClick={() => setSideTab(tab)}
              >
                {tab === "files" ? "Files" : tab === "outline" ? "Outline" : "Claims"}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`tex-panel-${sideTab}`} aria-labelledby={`tex-tab-${sideTab}`}>
            {sideTab === "files" ? (
              <ul className="tex-files" data-testid="file-tree">
                {(files.data?.files ?? []).map((f) => (
                  <li key={f.path}>
                    <button
                      type="button"
                      className="link-button"
                      aria-current={file?.path === f.path ? "true" : undefined}
                      disabled={!f.text}
                      onClick={() => void openPath(f.path)}
                    >
                      {f.path}
                    </button>{" "}
                    <span className="muted small-print">{STATE_TEXT[f.state] ?? f.state}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {sideTab === "outline" ? (
              <ol className="tex-outline" data-testid="outline">
                {(structure.data?.outline ?? []).map((o) => (
                  <li key={`${o.file}:${o.line}`} className={`tex-level-${o.level}`}>
                    <button type="button" className="link-button" onClick={() => void openPath(o.file, o.line)}>
                      {o.title}
                    </button>{" "}
                    <span className="muted small-print">
                      {o.file}:{o.line}
                    </span>
                  </li>
                ))}
              </ol>
            ) : null}
            {sideTab === "claims" ? (
              <ul className="tex-claims" data-testid="claims-list">
                {located.length === 0 ? <li className="muted">No anchors bind claims to this manuscript.</li> : null}
                {located.map(({ anchor, line }) => (
                  <li key={anchor.id} data-testid="claim-anchor">
                    <p>{anchorLabel(anchor)}</p>
                    {line !== null ? (
                      <button type="button" className="button secondary small" onClick={() => void openPath(main ?? "", line)}>
                        Go to line {line}
                      </button>
                    ) : (
                      <p className="muted small-print">
                        {anchor.status === "needs_review"
                          ? `Needs review: ${anchor.reason ?? ""}`
                          : "The anchored text is not in the current file exactly once."}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </aside>
        <div className="tex-editor-pane">
          {file ? (
            <TexEditor
              ref={editor}
              docKey={`${file.path}#${file.loadId}`}
              initial={file.text}
              initialLine={file.line}
              crlf={file.crlf}
              label={`Editing ${file.path}`}
              completion={completion}
              marks={marks}
              onChange={(text) => setFile((f) => (f ? { ...f, text } : f))}
              onSave={() => void save()}
              onForward={() => void forward()}
            />
          ) : (
            <Loading label="Loading the file" />
          )}
          <p className="muted small-print">
            Ctrl+S saves, Ctrl+F finds and replaces, Ctrl+Space completes labels and citation
            keys, Ctrl+Alt+J shows the cursor line in the PDF. Tab leaves the editor.
          </p>
          <EvidenceInsert
            paperBase={base}
            onInserted={(result) => {
              editor.current?.insert(result.snippet);
              files.reload();
              structure.reload();
              setNotice(
                result.key
                  ? `Inserted \\cite{${result.key}}${result.written ? ` and added it to ${result.bib_file}` : ""}.`
                  : `Inserted the figure ${result.file ?? ""}.`,
              );
            }}
          />
        </div>
        <div className="tex-pdf-pane" aria-label="Compiled PDF" role="region" tabIndex={0}>
          <CompileSummary state={compileState} status={status} last={lastCompile} />
          <DiagnosticsPanel diagnostics={diagnostics} onJump={(d) => d.file && void openPath(d.file, d.line ?? 1)} />
          {pdf ? (
            <PdfView versionId={pdf.version_id} highlight={highlight} onInverse={(p, x, y) => void inverse(p, x, y)} />
          ) : (
            <p className="muted">No compiled PDF yet.</p>
          )}
        </div>
      </div>
      <OverleafPanel paperBase={base} onPulled={() => {
        files.reload();
        structure.reload();
        if (fileRef.current && fileRef.current.text === fileRef.current.loaded) void reloadFromDisk();
      }} />
    </section>
  );
}

function CompileSummary({
  state,
  status,
  last,
}: {
  state: string | null;
  status: CompileStatus | undefined;
  last: CompileResult | null;
}) {
  const pdf = status?.pdf;
  const current = status?.last_compile?.pdf_current ?? false;
  return (
    <div className="tex-compile-summary" data-testid="compile-summary">
      {state ? (
        <p>
          Last compile: <strong data-testid="compile-state">{state}</strong>
          {last ? ` (${last.seconds.toFixed(1)} s)` : ""}
        </p>
      ) : (
        <p className="muted">Not compiled yet.</p>
      )}
      {pdf ? (
        <p className="small-print" data-testid="pdf-label">
          Showing the PDF compiled {formatDate(pdf.captured_at)} (version {pdf.version_id.slice(-8)}, from{" "}
          {pdf.transformation?.input_version_refs.length ?? 0} recorded input files).
        </p>
      ) : null}
      {pdf && status?.last_compile && !current ? (
        <p className="inline-warning" role="alert" data-testid="stale-pdf">
          The latest compile did not produce a PDF ({status.last_compile.status}). This is the
          previous PDF, built from older inputs.
        </p>
      ) : null}
    </div>
  );
}

function DiagnosticsPanel({ diagnostics, onJump }: { diagnostics: Diagnostic[]; onJump: (d: Diagnostic) => void }) {
  if (diagnostics.length === 0) return null;
  return (
    <section className="tex-diagnostics" aria-label="Compile diagnostics">
      <h2 className="subhead">Diagnostics</h2>
      <ul data-testid="diagnostics">
        {diagnostics.slice(0, 100).map((d, i) => (
          <li key={i} className={`tex-diag tex-diag-${d.severity}`}>
            <span className="badge">{d.severity}</span>{" "}
            {d.file ? (
              <button type="button" className="link-button" onClick={() => onJump(d)}>
                {d.file}:{d.line ?? "?"}
              </button>
            ) : null}{" "}
            {d.message}
          </li>
        ))}
      </ul>
    </section>
  );
}

function EnginePanel({ engine, reload }: { engine: EngineInfo | undefined; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!engine || engine.status.state === "installed") return null;
  const plan = engine.plan;
  return (
    <div className="inline-warning" data-testid="engine-panel">
      <p>
        <strong>The TeX engine is not installed.</strong> Install Tectonic {plan.version} ({plan.license}
        , {(plan.download_bytes / 1e6).toFixed(1)} MB, SHA-256 {plan.sha256.slice(0, 12)}…) into{" "}
        <code>{plan.destination}</code>. {plan.network}
      </p>
      {error ? <p className="field-error">{describeError(error).detail}</p> : null}
      <button
        type="button"
        className="button secondary small"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          runOperation("/tex/engine/install", { digest: plan.plan_digest })
            .then(() => reload())
            .catch((exc: unknown) => setError(exc))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? "Installing…" : `Install Tectonic ${plan.version}`}
      </button>
    </div>
  );
}

function EvidenceInsert({ paperBase, onInserted }: { paperBase: string; onInserted: (r: InsertResult) => void }) {
  const sources = useKindList<SourceBody>("source");
  const versions = useKindList<VersionBody>("version");
  const [choice, setChoice] = useState("");
  const [error, setError] = useState<unknown>(null);
  const options = useMemo(() => {
    const titles = new Map((sources.data?.items ?? []).map((s) => [s.id, s.body.title]));
    const latest = new Map<string, { id: string; mime: string; title: string }>();
    for (const v of versions.data?.items ?? []) {
      const title = titles.get(v.body.source_id);
      if (!title || title.startsWith("Compiled PDF") || title.startsWith("Compile log") || title.startsWith("SyncTeX map")) continue;
      latest.set(v.body.source_id, { id: v.id, mime: v.body.mime, title });
    }
    return [...latest.values()].sort((a, b) => a.title.localeCompare(b.title));
  }, [sources.data, versions.data]);
  const chosen = options.find((o) => o.id === choice);
  const insert = (kind: "cite" | "figure") => {
    setError(null);
    post<InsertResult>(`${paperBase}/workspace/insert`, { kind, version_id: choice })
      .then(onInserted)
      .catch((exc: unknown) => setError(exc));
  };
  const submit = (event: FormEvent) => event.preventDefault();
  return (
    <form className="tex-insert" onSubmit={submit} aria-label="Insert from recorded evidence">
      <label className="field">
        <span>Recorded source</span>
        <select value={choice} onChange={(e) => setChoice(e.target.value)} data-testid="evidence-select">
          <option value="">Choose a recorded source…</option>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.title}
            </option>
          ))}
        </select>
      </label>
      <div className="button-row">
        <button type="button" className="button secondary small" disabled={!choice} onClick={() => insert("cite")}>
          Insert citation
        </button>
        <button
          type="button"
          className="button secondary small"
          disabled={!chosen || !/^(image\/(png|jpeg)|application\/pdf)$/.test(chosen.mime)}
          onClick={() => insert("figure")}
        >
          Insert figure
        </button>
      </div>
      {error ? <p className="field-error">{describeError(error).detail}</p> : null}
    </form>
  );
}

// Text that others can write (commit subjects and authors, file names, remote URLs) is
// shown with its invisible characters (controls, bidi and other format characters,
// separators other than a plain space, unassigned code points) as escapes, so it reads as
// exactly what it is (pre-check rounds 2-3, INT2G-F2 and R2-L1).
const INVISIBLE = /(?! )[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/gu;

function visible(text: string): string {
  return text.replace(INVISIBLE, (c) => `\\u{${(c.codePointAt(0) ?? 0).toString(16)}}`);
}

// The destination bound into a pull or push plan (integration review INT2-F3). For a local
// repository the hub receives a label.
function destination(url: string | undefined): string {
  return url === undefined ? "a local repository (its path is not shown in the browser)" : visible(url);
}

function OverleafPanel({ paperBase, onPulled }: { paperBase: string; onPulled: () => void }) {
  const status = useQuery<OverleafStatus>(`${paperBase}/overleaf`);
  const [pull, setPull] = useState<OverleafPreview | null>(null);
  const [push, setPush] = useState<OverleafPreview | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);
  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    setDone(null);
    try {
      await fn();
    } catch (exc) {
      setError(exc);
    } finally {
      setBusy(null);
      status.reload();
    }
  };
  if (status.error && !status.data) {
    const notLinked = status.error instanceof ApiError && status.error.kind === "validation";
    return (
      <section className="section tex-overleaf" aria-labelledby="overleaf-title">
        <h2 id="overleaf-title">Overleaf</h2>
        {notLinked ? (
          <p className="muted">
            This workspace is not an Overleaf Git clone. Link one with{" "}
            <code>prometheos paper overleaf link</code> (the Overleaf token stays in Git
            Credential Manager).
          </p>
        ) : (
          <ErrorState error={status.error} onRetry={status.reload} />
        )}
      </section>
    );
  }
  const s = status.data;
  return (
    <section className="section tex-overleaf" aria-labelledby="overleaf-title" data-testid="overleaf-panel">
      <h2 id="overleaf-title">Overleaf</h2>
      {s ? (
        <p data-testid="overleaf-status">
          Branch <strong>{s.branch}</strong>
          {s.ahead !== null ? `: ${s.ahead} local commit(s) not on Overleaf, ${s.behind} Overleaf commit(s) not here (as of the last check)` : ""}
          . {s.local_changes.length} file(s) changed locally and not yet committed.
        </p>
      ) : (
        <Loading label="Reading the Git clone" />
      )}
      {s && s.blocked_settings.length > 0 ? (
        <p className="inline-warning" role="alert">
          PrometheOS will not fetch or push with these Git settings (they can run a program or send
          a fetch or push elsewhere): {s.blocked_settings.join(", ")}.
        </p>
      ) : null}
      <div className="button-row">
        <button
          type="button"
          className="button secondary small"
          disabled={busy !== null}
          onClick={() =>
            void act("pull", async () => {
              setPull(await post<OverleafPreview>(`${paperBase}/overleaf/pull/preview`, { fetch: true }));
            })
          }
        >
          Check Overleaf for changes
        </button>
        <button
          type="button"
          className="button secondary small"
          disabled={busy !== null}
          onClick={() =>
            void act("push", async () => {
              setPush(await post<OverleafPreview>(`${paperBase}/overleaf/push/preview`, {}));
            })
          }
        >
          Prepare a push
        </button>
      </div>
      {pull ? (
        <div className="card" data-testid="pull-preview">
          <p>
            <strong>{pull.plan.explanation}</strong>
          </p>
          <p className="small-print" data-testid="pull-source">
            From <code>{destination(pull.plan.remote_url)}</code>
          </p>
          {(pull.plan.incoming_commits ?? []).length > 0 ? (
            <ul>
              {(pull.plan.incoming_commits ?? []).map((c) => (
                <li key={c.oid}>
                  {visible(c.subject)} <span className="muted">({visible(c.author)})</span>
                </li>
              ))}
            </ul>
          ) : null}
          {(pull.plan.incoming_files ?? []).length > 0 ? (
            <p className="small-print">Files: {(pull.plan.incoming_files ?? []).map((f) => visible(f.path)).join(", ")}</p>
          ) : null}
          {(pull.plan.conflicts ?? []).length > 0 ? (
            <p className="small-print">Conflicting files: {(pull.plan.conflicts ?? []).map(visible).join(", ")}</p>
          ) : null}
          {pull.applicable ? (
            <button
              type="button"
              className="button primary small"
              disabled={busy !== null}
              onClick={() =>
                void act("pull-apply", async () => {
                  const result = await post<{ applied: string }>(`${paperBase}/overleaf/pull/apply`, {
                    digest: pull.digest,
                  });
                  setPull(null);
                  setDone(`Pulled (${result.applied.replace("_", "-")}); changed files were recorded as new versions.`);
                  onPulled();
                })
              }
            >
              Pull these changes
            </button>
          ) : null}
        </div>
      ) : null}
      <form
        className="tex-commit"
        onSubmit={(event) => {
          event.preventDefault();
          void act("commit", async () => {
            await post(`${paperBase}/overleaf/commit`, { message });
            setMessage("");
            setDone("Committed locally. Nothing has been sent to Overleaf.");
          });
        }}
      >
        <label className="field">
          <span>Commit message for local changes</span>
          <input value={message} onChange={(e) => setMessage(e.target.value)} maxLength={500} />
        </label>
        <button type="submit" className="button secondary small" disabled={!message.trim() || busy !== null}>
          Commit locally
        </button>
      </form>
      {push ? (
        <div className="card" data-testid="push-preview">
          <p>
            <strong>{push.plan.explanation}</strong>
          </p>
          <p data-testid="push-destination">
            Destination: <code>{destination(push.plan.remote_url)}</code>
          </p>
          {(push.plan.restricted_files ?? []).length > 0 ? (
            <p className="inline-warning" role="alert" data-testid="push-restricted">
              These files are marked {""}
              {(push.plan.restricted_files ?? []).map((f) => `${visible(f.path)} (${f.sensitivity})`).join(", ")}
              ; pushing sends them to Overleaf.
            </p>
          ) : null}
          <ul>
            {(push.plan.commits ?? []).map((c) => (
              <li key={c.oid}>
                {visible(c.subject)} <span className="muted">({c.oid.slice(0, 10)})</span>
              </li>
            ))}
          </ul>
          {push.applicable ? (
            <button
              type="button"
              className="button primary small"
              disabled={busy !== null}
              onClick={() =>
                void act("push-apply", async () => {
                  await post(`${paperBase}/overleaf/push/apply`, { digest: push.digest });
                  setPush(null);
                  setDone(`Pushed exactly the reviewed commits to ${destination(push.plan.remote_url)} (no force).`);
                })
              }
            >
              Approve and push exactly these {(push.plan.commits ?? []).length} commit(s)
            </button>
          ) : null}
        </div>
      ) : null}
      {done ? (
        <p className="success" role="status" data-testid="overleaf-done">
          {done}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      <ul className="limitations small-print">
        {(s?.limitations ?? []).map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </section>
  );
}
