// Shapes of the CP9 workspace, compile and Overleaf routes (decision 0012).

export interface WorkspaceInfo {
  paper_id: string;
  paper_rev: number;
  title: string;
  manuscript_source_id: string;
  root_alias: string;
  folder: string;
  main: string;
}

export interface WorkspaceFile {
  path: string;
  size: number;
  sha256: string | null;
  text: boolean;
  compile_input: boolean;
  source_id: string | null;
  latest_version_id: string | null;
  state: "unversioned" | "versioned" | "changed";
}

export interface WorkspaceFiles {
  workspace: WorkspaceInfo;
  files: WorkspaceFile[];
  skipped: Record<string, number>;
  limitations: string[];
}

export interface FileContent {
  workspace: WorkspaceInfo;
  path: string;
  sha256: string;
  size: number;
  text: string | null;
  line_ending: "lf" | "crlf";
  binary: boolean;
}

export interface Located {
  key: string;
  file: string;
  line: number;
  type?: string;
  command?: string;
}

export interface OutlineItem {
  level: number;
  command: string;
  title: string;
  file: string;
  line: number;
}

export interface GutterAnchor {
  id: string;
  rev: number;
  status: "current" | "needs_review" | "resolved";
  reason: string | null;
  manuscript_version_id: string;
  locator: { type: string; start_line?: number; end_line?: number };
  anchored_lines: string[] | null;
  claims: { id: string; rev: number; statement: string | null; state: string | null }[];
}

export interface Structure {
  workspace: WorkspaceInfo;
  labels: Located[];
  refs: Located[];
  cites: Located[];
  bib_keys: Located[];
  outline: OutlineItem[];
  undefined_refs: string[];
  undefined_cites: string[];
  anchors: GutterAnchor[];
}

export interface Diagnostic {
  severity: "error" | "warning" | "info";
  file: string | null;
  line: number | null;
  message: string;
  origin: string;
  context: string;
}

export interface OutputVersion {
  version_id: string;
  source_id: string;
  commit_seq: number;
  captured_at: string;
  sha256: string;
  byte_length: number;
  transformation: { input_version_refs: string[]; engine: string; engine_version: string } | null;
}

export interface CompileStatus {
  paper_id: string;
  project_seq: number;
  last_compile: {
    status: string | null;
    log: OutputVersion;
    pdf_current: boolean;
    diagnostics: Diagnostic[];
  } | null;
  pdf: OutputVersion | null;
  synctex: OutputVersion | null;
}

export interface CompileResult {
  status: string;
  diagnostics: Diagnostic[];
  outputs: Record<string, string>;
  inputs: Record<string, string>;
  seconds: number;
}

export interface ForwardResult {
  pdf_version_id: string;
  target: {
    page: number;
    line: number;
    file: string;
    rects: { x: number; y: number; width: number; height: number }[];
  } | null;
}

export interface InverseResult {
  pdf_version_id: string;
  source: { file: string; line: number } | null;
}

export interface EngineInfo {
  status: { state: string; pinned_version: string; problem: string | null; cache: { bytes: number; files: number }; network: string };
  plan: {
    version: string;
    download_bytes: number;
    license: string;
    destination: string;
    sha256: string;
    network: string;
    plan_digest: string;
  };
}

export interface OverleafStatus {
  remote: { name: string; url: string | null; url_accepted: boolean };
  branch: string;
  head: string | null;
  remote_head_as_of_last_fetch: string | null;
  ahead: number | null;
  behind: number | null;
  local_changes: string[];
  blocked_settings: string[];
  limitations: string[];
}

export interface Commit {
  oid: string;
  author: string;
  date: string;
  subject: string;
}

export interface OverleafPreview {
  plan: {
    kind: string;
    explanation: string;
    incoming_commits?: Commit[];
    incoming_files?: { status: string; path: string }[];
    conflicts?: string[];
    both_changed?: string[];
    commits?: Commit[];
    files?: { status: string; path: string }[];
    uncommitted_not_pushed?: string[];
    restricted_files?: { path: string; sensitivity: string }[];
    // The one origin URL the plan's digest binds and the fetch and push name.
    remote_url?: string;
  };
  digest: string;
  applicable: boolean;
  limitations: string[];
}

export interface InsertResult {
  snippet: string;
  written: boolean;
  key?: string;
  file?: string;
  bib_file?: string;
}
