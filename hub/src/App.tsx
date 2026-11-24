import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { ApiError, get } from "./api";
import { ContextPanel } from "./components/ContextPanel";
import { CopyButton, Loading } from "./components/ui";
import { useHub, useQuery } from "./hub";
import { CapturePage } from "./pages/Capture";
import { CorrectionPage } from "./pages/Correction";
import { KnowledgePage } from "./pages/Knowledge";
import { LibraryPage } from "./pages/Library";
import { MatrixPage } from "./pages/Matrix";
import { MeetingPage } from "./pages/Meeting";
import { ActionPage } from "./pages/Action";
import { AdoptionPage } from "./pages/Adoption";
import { ApprovalsPage } from "./pages/Approvals";
import { ArtifactsPage } from "./pages/Artifacts";
import { NotebookPage } from "./pages/Notebook";
import { NotFoundPage } from "./pages/NotFound";
import { NowPage } from "./pages/Now";
import { PairingPage } from "./pages/Pairing";
import { RecordingBanner, TranscriptPage } from "./pages/Audio";
import { PaperPage } from "./pages/Paper";
import { PapersPage } from "./pages/Papers";
import { ProviderPage } from "./pages/Provider";
import { RecoveryPage } from "./pages/Recovery";
import { RunsPage } from "./pages/Runs";
import { WorkPage } from "./pages/Work";
import { SkillPage, SkillsPage, WorkflowPage } from "./pages/Skills";
import { CommandLayer, openOverlay } from "./components/Palette";
import { Icon } from "./components/icons";
import { DESTINATIONS, OPERATE, SYSTEM, isCurrent, type NavEntry } from "./nav";
import { Link, match, useRouter } from "./router";
import { THEME_LABELS, nextTheme, useTheme } from "./theme";
import type { ContextPack } from "./types";

// The LaTeX workspace (CodeMirror and PDF.js) loads only when it is opened.
const WorkspacePage = lazy(() =>
  import("./pages/Workspace").then((m) => ({ default: m.WorkspacePage })),
);

function Route() {
  const { path } = useRouter();
  if (path === "/" || path === "/now") return <NowPage />;
  if (path === "/work") return <WorkPage />;
  if (path === "/notebook") return <NotebookPage />;
  if (path === "/knowledge") return <KnowledgePage />;
  if (path === "/knowledge/library") return <LibraryPage />;
  if (path === "/knowledge/matrix") return <MatrixPage />;
  if (path === "/papers") return <PapersPage />;
  if (path === "/capture") return <CapturePage />;
  if (path === "/skills") return <SkillsPage />;
  if (path === "/ai") return <ProviderPage />;
  if (path === "/approvals") return <ApprovalsPage />;
  if (path === "/artifacts") return <ArtifactsPage />;
  if (path === "/runs") return <RunsPage />;
  if (path === "/adoption") return <AdoptionPage />;
  if (path === "/recovery") return <RecoveryPage />;
  let params = match("/approvals/:id", path);
  if (params?.id) return <ApprovalsPage focus={params.id} />;
  params = match("/runs/:id", path);
  if (params?.id) return <RunsPage focus={params.id} />;
  params = match("/meetings/:id", path);
  if (params?.id) return <MeetingPage id={params.id} />;
  params = match("/corrections/:id", path);
  if (params?.id) return <CorrectionPage id={params.id} />;
  params = match("/actions/:id", path);
  if (params?.id) return <ActionPage id={params.id} />;
  params = match("/skills/:ref", path);
  if (params?.ref) return <SkillPage skillRef={params.ref} />;
  params = match("/papers/:id/workspace", path);
  if (params?.id)
    return (
      <Suspense fallback={<Loading label="Loading the LaTeX workspace" />}>
        <WorkspacePage id={params.id} />
      </Suspense>
    );
  params = match("/papers/:id", path);
  if (params?.id) return <PaperPage id={params.id} />;
  params = match("/transcripts/:id", path);
  if (params?.id) return <TranscriptPage id={params.id} />;
  params = match("/workflows/:id", path);
  if (params?.id) return <WorkflowPage id={params.id} />;
  return <NotFoundPage />;
}

function CoverageWarning() {
  const context = useQuery<ContextPack>("/context?purpose=orient&budget=2000");
  const coverage = context.data?.manifest.coverage;
  if (!coverage) return null;
  const problems = [
    [coverage.missing_versions.length, "missing source version(s)"],
    [coverage.changed_origins.length, "source(s) changed at origin"],
    [coverage.unavailable_roots.length, "research folder(s) unavailable"],
    // Reference-only sources are a retention choice, not a coverage problem: Artifacts shows
    // them, and they warn here only once they go missing or change (decision 0078, P35).
  ].filter(([count]) => Number(count) > 0);
  if (problems.length === 0) return null;
  return (
    <p className="coverage-warning" data-testid="coverage-warning">
      <span aria-hidden="true">⚠ </span>
      <strong>Source coverage:</strong> {problems.map(([n, label]) => `${n} ${label}`).join("; ")}.
    </p>
  );
}

function NavLink({ entry, count, countLabel }: { entry: NavEntry; count?: number; countLabel?: string }) {
  const { path } = useRouter();
  const current = isCurrent(entry.to, path);
  const countId = `nav-count-${entry.icon}`;
  return (
    <li>
      <Link
        to={entry.to}
        className="nav-link"
        aria-current={current ? "page" : undefined}
        aria-describedby={count ? countId : undefined}
        title={entry.label}
        data-testid={entry.to === "/recovery" ? "header-recovery" : undefined}
      >
        <Icon name={entry.icon} size={17} />
        <span className="nav-label">{entry.label}</span>
        {count ? (
          <span className="nav-count" aria-hidden="true" data-testid={`nav-count-${entry.icon}`}>
            {count > 99 ? "99+" : count}
          </span>
        ) : null}
      </Link>
      {count ? (
        <span id={countId} className="visually-hidden">
          {count} {countLabel}
        </span>
      ) : null}
    </li>
  );
}

function LedgerStatus() {
  const { offline, project } = useHub();
  return (
    <p
      className={offline ? "ledger-status offline" : "ledger-status"}
      data-testid="ledger-status"
      title={offline ? "Offline, retrying" : project ? "Ledger connected" : "Connecting"}
    >
      <span className="ledger-dot" aria-hidden="true" />
      <span className="nav-label">{offline ? "Offline, retrying" : project ? "Ledger connected" : "Connecting"}</span>
    </p>
  );
}

function ThemeButton() {
  const [theme, set] = useTheme();
  const next = nextTheme(theme);
  return (
    <button
      type="button"
      className="side-button"
      onClick={() => set(next)}
      aria-label={`Theme: ${THEME_LABELS[theme]}. Switch to ${THEME_LABELS[next]}`}
      title={`Theme: ${THEME_LABELS[theme]}`}
      data-testid="theme-toggle"
      data-theme-choice={theme}
    >
      <Icon name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "monitor"} size={17} />
      <span className="nav-label">{THEME_LABELS[theme]}</span>
    </button>
  );
}

const ACTIVE_RUN = ["queued", "running", "waiting_for_input", "paused_budget"];
const COUNTS_POLL_MS = 20000;
const COUNTS_DELAY_MS = 1500;

/** The sidebar's counts: read on each page change and every 20 s while visible, not on every
 *  change-feed bump, so a busy project (an import, a scan) is not slowed by extra reads. */
function useSidebarCounts(): { pending: number; active: number } {
  const { status } = useHub();
  const { path } = useRouter();
  const [counts, setCounts] = useState({ pending: 0, active: 0 });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") setTick((t) => t + 1);
    }, COUNTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (status !== "paired") return;
    const controller = new AbortController();
    // Read after the page's own burst of reads, so the counts never compete with it for the
    // project store (each read holds the store's admission gate briefly).
    const timer = window.setTimeout(() => {
      Promise.allSettled([
        get<{ approvals: { state: string }[] }>("/approvals", { signal: controller.signal }),
        get<{ runs: { state: string }[] }>("/runs?limit=20", { signal: controller.signal }),
      ]).then(([approvals, runs]) => {
        if (controller.signal.aborted) return;
        setCounts((prior) => ({
          pending:
            approvals.status === "fulfilled"
              ? approvals.value.approvals.filter((a) => a.state === "pending").length
              : prior.pending,
          active:
            runs.status === "fulfilled" ? runs.value.runs.filter((r) => ACTIVE_RUN.includes(r.state)).length : prior.active,
        }));
      });
    }, COUNTS_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [status, path, tick]);
  return counts;
}

function Sidebar() {
  const { logout } = useHub();
  const { pending, active } = useSidebarCounts();
  return (
    <aside className="sidebar" aria-label="Hub navigation">
      <div className="brand">
        <Icon name="flame" size={20} className="brand-mark" />
        <span className="nav-label brand-name">PrometheOS</span>
      </div>
      <nav aria-label="Primary" className="nav-group primary-nav">
        <p className="nav-group-label" aria-hidden="true">
          Research
        </p>
        <ul>
          {DESTINATIONS.map((d) => (
            <NavLink key={d.to} entry={d} />
          ))}
        </ul>
      </nav>
      <nav aria-label="Project tools" className="nav-group">
        <p className="nav-group-label" aria-hidden="true">
          Operate
        </p>
        <ul>
          {OPERATE.map((d) => (
            <NavLink
              key={d.to}
              entry={d}
              count={d.to === "/approvals" ? pending : d.to === "/runs" ? active : undefined}
              countLabel={d.to === "/approvals" ? "waiting for you" : "running"}
            />
          ))}
        </ul>
        <p className="nav-group-label" aria-hidden="true">
          System
        </p>
        <ul>
          {SYSTEM.map((d) => (
            <NavLink key={d.to} entry={d} />
          ))}
        </ul>
      </nav>
      <div className="sidebar-foot">
        <LedgerStatus />
        <ThemeButton />
        <button type="button" className="side-button" onClick={() => void logout()} title="Sign out">
          <Icon name="signout" size={17} />
          <span className="nav-label">Sign out</span>
        </button>
      </div>
    </aside>
  );
}

function AgentGuide() {
  const { project } = useHub();
  // The guide's commands name this project: with several registered, a command without
  // --project would fail (decision 0054).
  const projectRef = project?.project_id ?? "<project id>";
  const pluginCommand = `prometheos host setup --host claude-code-plugin --project ${projectRef} --target <folder>`;
  const folderCommand = `prometheos host setup --host claude-code --project ${projectRef} --target <folder>`;
  return (
    <details className="agent-guide">
      <summary>
        <Icon name="plug" />
        <span className="topbar-label">Connect an agent</span>
      </summary>
      <div className="agent-guide-body" data-testid="agent-guide-body">
        <p>
          Your coding agent stays the place where work runs. To connect it to this project,
          run one of these in a terminal and review the plan before applying it:
        </p>
        <p className="agent-guide-label">Claude Code, as a plugin:</p>
        <div className="command-row">
          <pre className="command">{pluginCommand}</pre>
          <CopyButton text={pluginCommand} label="Copy the plugin setup command" />
        </div>
        <p className="muted">
          Then, in Claude Code: <code>/plugin marketplace add &lt;folder&gt;</code> and{" "}
          <code>/plugin install prometheos@prometheos-local</code>.
        </p>
        <p className="agent-guide-label">Or inside a coding project folder:</p>
        <div className="command-row">
          <pre className="command">{folderCommand}</pre>
          <CopyButton text={folderCommand} label="Copy the project folder setup command" />
        </div>
        <p className="muted">
          Use <code>--host opencode</code> for OpenCode.
        </p>
        <p className="muted">
          Add <code>--apply</code> to write the planned files. Nothing is installed from this
          page. See docs/agent-package.md.
        </p>
      </div>
    </details>
  );
}

function Topbar() {
  const { project, projectError, offline } = useHub();
  return (
    <header className="topbar">
      <div className="topbar-row">
        <div className="project-id">
          <p className="project-title">{project?.settings.title ?? (projectError ? "Project unavailable" : "Loading project…")}</p>
          {project ? (
            <p className="project-objective" title={project.settings.current_objective}>
              <span className="muted">Objective: </span>
              {project.settings.current_objective}
            </p>
          ) : null}
        </div>
        <div className="header-tools">
          <button
            type="button"
            className="palette-trigger"
            onClick={() => openOverlay("palette")}
            aria-keyshortcuts="Control+K"
            data-testid="palette-trigger"
          >
            <Icon name="search" />
            <span className="topbar-label">Jump to anything…</span>
            <kbd className="kbd">Ctrl K</kbd>
          </button>
          <AgentGuide />
          <Link to="/capture" className="button primary small header-capture">
            <Icon name="plus" />
            Capture
          </Link>
        </div>
      </div>
      <CoverageWarning />
      <RecordingBanner />
      {offline ? (
        <p className="offline-banner" role="alert" data-testid="offline-banner">
          <span aria-hidden="true">⚠ </span>
          Offline: the hub cannot be reached. Drafts stay on this page; retrying automatically.
        </p>
      ) : null}
    </header>
  );
}

/** A02, CP10.5: when the project store does not open, the recovery screen appears before any
 *  destination loads (spec/06). */
function RecoveryShell() {
  const { logout, maintenance } = useHub();
  return (
    <div className="app app-recovery">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="topbar">
        <div className="topbar-row">
          <Icon name="flame" size={22} className="brand-mark" />
          <div className="project-id">
            <p className="project-title">Project recovery</p>
            <p className="project-objective muted">
              {maintenance ? "Maintenance mode: the project store is not opened." : "The project store cannot be read."}
            </p>
          </div>
          <div className="header-tools">
            <button type="button" className="button ghost small" onClick={() => void logout()}>
              <Icon name="signout" />
              Sign out
            </button>
          </div>
        </div>
      </header>
      <div className="workspace">
        <main id="main" tabIndex={-1}>
          <RecoveryPage />
        </main>
      </div>
    </div>
  );
}

function needsRecovery(error: unknown): boolean {
  return error instanceof ApiError && "recovery" in error.details;
}

export function App() {
  const { status, source, maintenance, projectError } = useHub();
  const { path } = useRouter();
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);

  // Move focus to the new page heading on client-side navigation.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const heading = document.getElementById("page-title");
    (heading ?? mainRef.current)?.focus();
    window.scrollTo(0, 0);
    // Replay the short settle-in on each page change (off under reduced motion).
    const main = mainRef.current;
    if (main) {
      main.classList.remove("page-enter");
      void main.offsetWidth;
      main.classList.add("page-enter");
    }
  }, [path]);

  if (status === "checking") {
    return (
      <main className="centered" id="main">
        <Loading label="Connecting to the hub" />
      </main>
    );
  }
  if (status === "unpaired") return <PairingPage />;
  if (maintenance || needsRecovery(projectError)) return <RecoveryShell />;

  return (
    <div className={source ? "app with-panel" : "app"}>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <Sidebar />
      <div className="app-main">
        <Topbar />
        <div className="workspace">
          <main id="main" tabIndex={-1} ref={mainRef} className="page-enter">
            <Route />
          </main>
          <ContextPanel />
        </div>
      </div>
      <CommandLayer />
    </div>
  );
}
