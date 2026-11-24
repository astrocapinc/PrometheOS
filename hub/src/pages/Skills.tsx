// Skills and workflow runs (contextual pages). Inspection shows provenance, licences and
// recipes; the install preview is a plan computed against an empty temporary folder:
// nothing is written to a project and nothing executes.
import { useState, type FormEvent } from "react";
import { describeError, post } from "../api";
import {
  Details,
  Empty,
  ErrorState,
  KeyValues,
  PageHeading,
  QueryView,
  StateBadge,
  formatDate,
} from "../components/ui";
import { SkillImport, SkillOwnerActions, WorkflowExport } from "../components/parity";
import { useKindList } from "../data";
import { useHub, useQuery } from "../hub";
import { Link, useRouter } from "../router";
import type { Envelope, InstallPlan, SkillDetail, SkillSummary } from "../types";

const TRUST_LABELS: Record<string, string> = {
  automated_checks_passed: "Automated checks passed",
  license_reviewed: "Licences reviewed",
  origin_identified: "Origin identified",
  live_use_observed: "Observed in live use",
  human_review: "Human review",
};

function TrustList({ trust }: { trust: Record<string, boolean | string | null> }) {
  return (
    <ul className="trust-list">
      {Object.entries(trust).map(([key, value]) => (
        <li key={key}>
          <span aria-hidden="true">{value === true ? "✓" : value === false ? "✗" : "–"} </span>
          {TRUST_LABELS[key] ?? key.replace(/_/g, " ")}:{" "}
          {value === true ? "yes" : value === false ? "no" : value === null ? "not recorded" : String(value)}
        </li>
      ))}
    </ul>
  );
}

export function SkillsPage() {
  const catalog = useQuery<{ packages: SkillSummary[]; denylist: unknown[] }>("/skills");
  const runs = useKindList<{ recipe_id: string; status: string }>("run");
  const { navigate } = useRouter();
  const [runId, setRunId] = useState("");
  const open = (event: FormEvent) => {
    event.preventDefault();
    if (runId.trim()) navigate(`/workflows/${encodeURIComponent(runId.trim())}`);
  };
  return (
    <>
      <PageHeading
        title="Skills"
        lead="Skill packages in your local catalog. Inspect before installing; nothing here executes."
      />
      <section aria-labelledby="catalog-title" className="section">
        <h2 id="catalog-title">Catalog</h2>
        <QueryView
          query={catalog}
          loadingLabel="Loading catalog"
          isEmpty={(d) => d.packages.length === 0}
          empty={
            <>
              No packages imported. Import one with <code>prometheos skill plan</code> and{" "}
              <code>prometheos skill import</code>.
            </>
          }
        >
          {(data) => (
            <ul className="card-list" data-testid="skill-list">
              {data.packages.map((p) => (
                <li key={p.name} className="card">
                  <div className="card-head">
                    <h3 className="card-title">
                      <Link to={`/skills/${encodeURIComponent(p.name)}`}>{p.name}</Link>{" "}
                      <span className="muted">version {p.version}</span>
                    </h3>
                    <StateBadge state={p.status} prefix="Package status" />
                  </div>
                  <p className="card-meta">Imported {formatDate(p.imported_at)}</p>
                  <TrustList trust={p.trust} />
                </li>
              ))}
            </ul>
          )}
        </QueryView>
      </section>
      <SkillImport onDone={catalog.reload} />
      <section aria-labelledby="runs-title" className="section">
        <h2 id="runs-title">Workflow runs</h2>
        <p className="muted">Your coding agent reports progress through a recipe; the hub only shows it.</p>
        <QueryView
          query={runs}
          loadingLabel="Loading workflow runs"
          isEmpty={(d) => d.items.length === 0}
          empty={<>No workflow runs recorded in this project.</>}
        >
          {(data) => (
            <ul className="plain-list">
              {data.items.map((r) => (
                <li key={r.id} className="row">
                  <Link to={`/workflows/${r.id}`}>Recipe {r.body.recipe_id}</Link>{" "}
                  <StateBadge state={r.body.status} />
                </li>
              ))}
            </ul>
          )}
        </QueryView>
        <form className="search-form" onSubmit={open} aria-label="Open a workflow run by ID">
          <div className="field grow">
            <label htmlFor="run-id">Open a run by ID</label>
            <input id="run-id" value={runId} onChange={(e) => setRunId(e.target.value)} />
          </div>
          <button type="submit" className="button secondary">
            Open run
          </button>
        </form>
      </section>
    </>
  );
}

export function SkillPage({ skillRef }: { skillRef: string }) {
  const detail = useQuery<SkillDetail>(`/skills/${encodeURIComponent(skillRef)}`);
  return (
    <>
      <PageHeading title={`Skill: ${skillRef}`} />
      <p>
        <Link to="/skills">All skills</Link>
      </p>
      <QueryView query={detail} loadingLabel="Loading skill">
        {(d) => <SkillView detail={d} skillRef={skillRef} onChange={detail.reload} />}
      </QueryView>
    </>
  );
}

function SkillView({
  detail,
  skillRef,
  onChange,
}: {
  detail: SkillDetail;
  skillRef: string;
  onChange: () => void;
}) {
  const files = Object.entries(detail.files);
  const licences = [...new Set(files.map(([, f]) => f.license ?? "unresolved"))];
  return (
    <>
      <p className="lead">{detail.description}</p>
      <p>
        <StateBadge state={detail.status} prefix="Package status" /> · {detail.package}
      </p>
      <section aria-labelledby="prov-title" className="section">
        <h2 id="prov-title">Provenance and licence</h2>
        <KeyValues
          rows={[
            ["Origin", String(detail.origin.kind ?? "unknown")],
            ["Licences", <span key="l" data-testid="skill-licences">{licences.join(", ")}</span>],
            ["Licence information", detail.license_complete ? "Complete for every file" : "Incomplete"],
            ["File digests", detail.digests_ok ? "Match the package record" : "Do not match"],
            ["Capabilities requested", detail.capabilities.join(", ") || "none"],
            ["Problems found", detail.problems.length ? String(detail.problems.length) : "none"],
          ]}
        />
        <TrustList trust={detail.trust} />
        <p className="muted">{detail.note}</p>
        <Details label="Files and digests">
          <table className="files-table">
            <caption className="visually-hidden">Package files</caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Licence</th>
                <th scope="col">Executable</th>
                <th scope="col">SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {files.map(([path, f]) => (
                <tr key={path}>
                  <td>
                    <code>{path}</code>
                  </td>
                  <td>{f.license ?? "unresolved"}</td>
                  <td>{f.executable ? "yes" : "no"}</td>
                  <td>
                    <code className="hash">{f.sha256}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Details>
      </section>
      <section aria-labelledby="recipes-title" className="section">
        <h2 id="recipes-title">Recipes</h2>
        {detail.recipes.length === 0 ? <Empty>This package declares no recipes.</Empty> : null}
        {detail.recipes.map((r) => (
          <article key={r.id} className="card recipe" data-testid="recipe">
            <h3 className="card-title">{r.title}</h3>
            <ol className="steps">
              {r.steps.map((s) => (
                <li key={s.id}>
                  <p>
                    {s.instruction}
                    {s.skippable ? <span className="muted"> (optional)</span> : null}
                  </p>
                  <p className="card-meta">
                    Expected artifact: {s.expected_artifacts.join(", ") || "none"} · Checks:{" "}
                    {s.checks.join("; ") || "none"}
                    {s.requires.length ? <> · After: {s.requires.join(", ")}</> : null}
                  </p>
                </li>
              ))}
            </ol>
            <Details>
              <p>
                Recipe ID: <code>{r.id}</code>
              </p>
            </Details>
          </article>
        ))}
      </section>
      <InstallPreview skillRef={skillRef} />
      <SkillOwnerActions
        skillRef={skillRef}
        status={detail.status}
        packageDigest={detail.package_digest}
        onDone={onChange}
      />
    </>
  );
}

function InstallPreview({ skillRef }: { skillRef: string }) {
  const { announce } = useHub();
  const [host, setHost] = useState("claude-code");
  const [plan, setPlan] = useState<InstallPlan["plan"] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const preview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const reply = await post<InstallPlan>(`/skills/${encodeURIComponent(skillRef)}/install-preview`, {
        host,
      });
      setPlan(reply.plan);
      announce(`Install preview ready: ${reply.plan.operations.length} file(s) would be created. Nothing was written.`);
    } catch (err) {
      setPlan(null);
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="install-title" className="section">
      <h2 id="install-title">Preview a project-local install</h2>
      <form onSubmit={preview} className="search-form">
        <div className="field">
          <label htmlFor="install-host">Coding agent</label>
          <select id="install-host" value={host} onChange={(e) => setHost(e.target.value)}>
            <option value="claude-code">Claude Code</option>
            <option value="opencode">OpenCode</option>
          </select>
        </div>
        <button type="submit" className="button primary" disabled={busy} aria-busy={busy}>
          {busy ? "Planning…" : "Preview install"}
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {plan ? (
        <div className="install-plan" data-testid="install-plan">
          <p className="success">
            <span aria-hidden="true">✓ </span>Preview only: nothing was written and nothing ran.
          </p>
          <p>
            Installing {plan.package} for {plan.host} would create these files inside your coding
            project folder:
          </p>
          <ul className="plain-list">
            {plan.operations.map((op) => (
              <li key={op.path}>
                <span className="badge badge-kind">{op.action}</span> <code>{op.path}</code>
              </li>
            ))}
          </ul>
          <p>
            Executable files: {plan.executable_files.length || "none"} · Conflicts:{" "}
            {plan.conflicts.length || "none"} · Files with unknown rights:{" "}
            {plan.unknown_rights.length || "none"}
          </p>
          <p className="muted">{plan.note}</p>
          <p>To install, run this in a terminal from your coding project and review its plan:</p>
          <pre className="command">
            prometheos skill install {skillRef} --host {plan.host} --target &lt;folder&gt;
          </pre>
          <Details>
            <p>
              Plan digest (against an empty preview folder): <code>{plan.plan_sha256}</code>
            </p>
          </Details>
        </div>
      ) : null}
    </section>
  );
}

interface WorkflowStatus {
  run: Envelope<{
    recipe_id: string;
    status: string;
    steps: { id: string; status: string; reason: string | null }[];
  }>;
  recipe: { title: string } | null;
  ready_steps: string[];
  package_in_catalog: boolean;
}

export function WorkflowPage({ id }: { id: string }) {
  const status = useQuery<WorkflowStatus>(`/workflows/${encodeURIComponent(id)}`);
  return (
    <>
      <PageHeading title="Workflow run" />
      <p>
        <Link to="/skills">Skills and workflow runs</Link>
      </p>
      <QueryView query={status} loadingLabel="Loading workflow run">
        {(w) => (
          <>
            <p className="lead">
              {w.recipe?.title ?? `Recipe ${w.run.body.recipe_id}`} · <StateBadge state={w.run.body.status} />
            </p>
            {!w.package_in_catalog ? (
              <p className="inline-warning">The package for this run is no longer in the catalog.</p>
            ) : null}
            <ol className="steps">
              {w.run.body.steps.map((s) => (
                <li key={s.id}>
                  {s.id}: <StateBadge state={s.status} />
                  {s.reason ? <span className="muted"> — {s.reason}</span> : null}
                  {w.ready_steps.includes(s.id) ? <strong> (ready next)</strong> : null}
                </li>
              ))}
            </ol>
            <Details>
              <p>
                Run: <code>{w.run.id}</code>
              </p>
            </Details>
            <WorkflowExport id={id} />
          </>
        )}
      </QueryView>
    </>
  );
}

