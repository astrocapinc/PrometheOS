// CP10.6 app paths (decision 0022): the operations that CP10.6 completed on every transport also
// have a path in the hub. Each test drives the page as the owner would and re-reads the stored
// result through a separately paired client: a downloaded notebook, a package imported from an
// approved folder then revoked and released, a workflow run exported, a requirements draft, a
// bundle and a hand-off written into the research folder, a recurring job planned, added,
// disabled and listed, the model and credential forms guarded, a purge planned and committed, and
// the unreferenced-file check. The page checks are followed by an axe pass over the new panels.
import AxeBuilder from "@axe-core/playwright";
import { request as browserRequest, type Page } from "@playwright/test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Api, type HubInfo } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function serious(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

const researchRoot = (hub: HubInfo): string => (hub as unknown as { research_root: string }).research_root;

test("Owner root review shows the exact old and new paths while agent views redact them", async ({ paired: page, hub, api }) => {
  const info = await api.get<any>("/project");
  const oldPath = researchRoot(hub);
  const newPath = join(oldPath, "latex");
  const raw = { schema_version: 1, project_id: hub.project_id, request_id: crypto.randomUUID(), expected_seq: info.project_seq,
    operations: [{ op: "project_update", base_rev: info.settings_rev, settings: { ...info.settings,
      allowed_roots: [{ alias: "research", path: newPath }] } }] };
  const agent = await browserRequest.newContext({ baseURL: hub.url, extraHTTPHeaders: {
    Authorization: `Bearer ${(hub as unknown as { agent_token: string }).agent_token}` } });
  try {
    const asked = await agent.post("/api/v1/transactions", { data: raw });
    expect(asked.status()).toBe(403);
    const error = (await asked.json()).error;
    expect(error.code).toBe("approval_required");
    const id = error.details.approval_request_id;
    const agentView = await agent.get(`/api/v1/approvals/${id}`);
    const agentText = (await agentView.text()).replaceAll("\\\\", "\\");
    expect(agentText).not.toContain(oldPath);
    expect(agentText).not.toContain(newPath);
    await page.goto(`${hub.url}approvals/${id}`);
    await expect(page.getByTestId("root-changes")).toContainText(oldPath);
    await expect(page.getByTestId("root-changes")).toContainText(newPath);
    await expect(page.getByTestId("root-changes")).toContainText("→");
    expect(await serious(page)).toEqual([]);
    await page.getByTestId("approval-deny").click();
    const current = await api.get<any>("/project");
    expect(current.settings_rev).toBe(info.settings_rev);
  } finally { await agent.dispose(); }
});

test("Notebook: the whole notebook downloads as one file", async ({ paired: page, hub, api }) => {
  await page.goto(`${hub.url}notebook`);
  await expect(page.getByTestId("notebook-export")).toBeVisible();
  expect(await serious(page)).toEqual([]);
  const entries = ((await api.get<{ entries: unknown[] }>("/notebook")).entries as unknown[]).length;
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("notebook-export-button").click(),
  ]);
  expect(download.suggestedFilename()).toBe("notebook.md");
  const path = await download.path();
  const text = readFileSync(path, "utf-8");
  expect(text).toContain("# Notebook (project_seq");
  expect(text).toContain("Baseline imported");
  await expect(page.getByTestId("notebook-export-done")).toContainText(`${entries} entries`);
  await page.getByLabel("Format").selectOption("json");
  const [json] = await Promise.all([page.waitForEvent("download"), page.getByTestId("notebook-export-button").click()]);
  expect(json.suggestedFilename()).toBe("notebook.json");
  expect(JSON.parse(readFileSync(await json.path(), "utf-8")).entries).toHaveLength(entries);
});

test("Skills: import a package from an approved folder, revoke it, release it", async ({ paired: page, hub, api }) => {
  const name = `e2e-parity-skill`;
  // An agent-style scaffold into the approved research folder (the API an agent uses).
  await api.post("/skills/create", {
    name,
    description: "A synthetic package scaffolded for the browser test.",
    license: "MIT",
    root_alias: "research",
  });
  expect(existsSync(join(researchRoot(hub), name, "prometheos-package.json"))).toBe(true);

  await page.goto(`${hub.url}skills`);
  await expect(page.getByTestId("skill-import")).toBeVisible();
  expect(await serious(page)).toEqual([]);
  // A folder that is not a package: the plan shows why it cannot be imported, and the button stays off.
  await page.getByLabel("Path inside the folder").fill("latex");
  await page.getByTestId("skill-import-plan").click();
  const bad = page.getByTestId("skill-import-preview");
  await expect(bad).toContainText("Blocking problems");
  await page.getByTestId("skill-import-reviewed").check();
  await expect(page.getByTestId("skill-import-commit")).toBeDisabled();
  // The scaffolded package: plan, review, import exactly that plan.
  await page.getByLabel("Path inside the folder").fill(name);
  await page.getByTestId("skill-import-plan").click();
  const preview = page.getByTestId("skill-import-preview");
  await expect(preview).toContainText(`${name}`);
  await expect(preview).toContainText("Blocking problems");
  await expect(preview).toContainText("none");
  await expect(page.getByTestId("skill-import-commit")).toBeDisabled(); // not reviewed yet
  await page.getByTestId("skill-import-reviewed").check();
  await page.getByTestId("skill-import-commit").click();
  await expect(page.getByTestId("skill-imported")).toContainText(name);
  const catalog = await api.get<{ packages: { name: string; status: string }[] }>("/skills");
  const imported = catalog.packages.find((p) => p.name === name);
  expect(imported, "the package is in the catalogue").toBeTruthy();
  expect(imported!.status).toBe("available");

  // Revoke it from its page.
  await page.goto(`${hub.url}skills/${encodeURIComponent(name)}`);
  await expect(page.getByTestId("skill-owner-actions")).toContainText("Revoke this package");
  expect(await serious(page)).toEqual([]);
  await expect(page.getByTestId("skill-revoke")).toBeDisabled();
  await page.getByTestId("skill-owner-confirm").check();
  await page.getByTestId("skill-revoke").click();
  await expect(page.getByTestId("skill-owner-result")).toContainText("revoked");
  const revoked = (await api.get<{ packages: { name: string; status: string }[] }>("/skills")).packages.find(
    (p) => p.name === name,
  );
  expect(revoked!.status).toBe("revoked");
  // A revoked package is released only after the owner reviews it.
  await page.reload();
  await expect(page.getByTestId("skill-owner-actions")).toContainText("Release from quarantine");
  await page.getByTestId("skill-owner-confirm").check();
  await page.getByTestId("skill-trust").click();
  await expect(page.getByTestId("skill-owner-result")).toContainText("released");
  const released = (await api.get<{ packages: { name: string; status: string }[] }>("/skills")).packages.find(
    (p) => p.name === name,
  );
  expect(released!.status).not.toBe("revoked");
});

test("Workflow run: export the run with its recipe", async ({ paired: page, hub, api }) => {
  // The fixture's package, installed into a project folder with the install recorded in the project.
  const folder = "e2e-installed-project";
  await api.post("/skills/create", { name: "e2e-run-host", description: "Folder for the install.", license: "MIT", root_alias: "research" });
  const plan = await api.post<{ plan: { plan_sha256: string } }>(`/skills/${hub.seed.skill_ref}/install`, {
    host: "claude-code",
    root_alias: "research",
    folder: "e2e-run-host",
  });
  const installed = await api.post<{ record: { results: { id: string }[] } }>(`/skills/${hub.seed.skill_ref}/install`, {
    host: "claude-code",
    root_alias: "research",
    folder: "e2e-run-host",
    plan_sha256: plan.plan.plan_sha256,
    record_in_project: true,
  });
  void folder;
  const packageId = installed.record.results[0]!.id;
  const started = await api.post<{ results: { id: string }[] }>("/workflows", {
    package_id: packageId,
    recipe_id: "evidence-pass",
    inputs: { question: "Does the plant hold?" },
    request_id: crypto.randomUUID(),
  });
  const runId = started.results[0]!.id;
  await page.goto(`${hub.url}workflows/${runId}`);
  expect(await serious(page)).toEqual([]);
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByTestId("workflow-export").click()]);
  expect(download.suggestedFilename()).toBe("workflow-run.json");
  const document = JSON.parse(readFileSync(await download.path(), "utf-8"));
  expect(Object.keys(document).sort()).toEqual(["ready_steps", "recipe", "run"]);
  expect(document.run.id).toBe(runId);
  expect(document.recipe.id).toBe("evidence-pass");
  await expect(page.getByTestId("workflow-export-done")).toBeVisible();
});

test("Paper: requirements draft, bundle export and folder hand-off", async ({ paired: page, hub, api }) => {
  await page.goto(`${hub.url}papers/${hub.seed.paper_id}`);
  expect(await serious(page)).toEqual([]);
  // The draft shows rows copied from the venue guidance and saves nothing.
  const before = await api.seq();
  await page.getByTestId("requirements-draft-button").click();
  const rows = page.getByTestId("requirements-draft-rows");
  await expect(rows).toContainText("eight pages");
  expect(await api.seq()).toBe(before);

  // The bundle is one new file inside the approved folder.
  await page.getByLabel("New file (a .zip path inside the folder)").fill("e2e-bundle.zip");
  await page.getByTestId("bundle-export-button").click();
  await expect(page.getByTestId("bundle-export-done")).toContainText("e2e-bundle.zip");
  expect(existsSync(join(researchRoot(hub), "e2e-bundle.zip"))).toBe(true);
  await page.getByTestId("bundle-export-button").click(); // never overwrites
  await expect(page.getByRole("alert").first()).toBeVisible();

  // The hand-off compares first, then writes only what was reviewed.
  await page.getByTestId("handoff-compare").click();
  const plan = page.getByTestId("handoff-plan");
  await expect(plan).toContainText("manuscript.md");
  await expect(plan.locator('tr[data-action="new"]').first()).toBeVisible();
  const before2 = readdirSync(researchRoot(hub));
  expect(before2.some((n) => n.startsWith("prometheos-handoff-"))).toBe(false);
  await page.getByTestId("handoff-apply").click();
  await expect(page.getByTestId("handoff-done")).toContainText("Nothing was deleted");
  expect(readdirSync(researchRoot(hub)).some((n) => n.startsWith("prometheos-handoff-"))).toBe(true);
  expect(await serious(page)).toEqual([]);
});

test("Papers: plan, add, disable and list a recurring job", async ({ paired: page, hub, api }) => {
  const before = (await api.get<{ jobs: unknown[] }>("/jobs")).jobs.length;
  await page.goto(`${hub.url}papers`);
  await expect(page.getByTestId("jobs")).toBeVisible();
  expect(await serious(page)).toEqual([]);
  await page.getByLabel("Every (minutes, at least 15)").fill("120");
  await page.getByTestId("job-plan").click();
  const preview = page.getByTestId("job-plan-view");
  await expect(preview).toContainText("Next due");
  await expect(preview).toContainText("prometheos jobs tick");
  expect((await api.get<{ jobs: unknown[] }>("/jobs")).jobs).toHaveLength(before); // a preview adds nothing
  await page.getByTestId("job-add").click();
  await expect(page.getByTestId("job-card")).toHaveCount(before + 1);
  const jobs = (await api.get<{ jobs: { job: { id: string; body: { schedule: { minutes: number }; enabled: boolean } } }[] }>("/jobs")).jobs;
  const added = jobs.find((j) => j.job.body.schedule.minutes === 120);
  expect(added, "the job is stored").toBeTruthy();
  const card = page.locator(`[data-testid="job-card"][data-job-id="${added!.job.id}"]`);
  await card.getByTestId("job-toggle").click();
  await expect(card).toHaveAttribute("data-enabled", "false");
  const stored = await api.get<{ body: { enabled: boolean } }>(`/records/${added!.job.id}`);
  expect(stored.body.enabled).toBe(false);
  await card.getByTestId("job-slots").click();
  await expect(card.getByTestId("job-slot-list")).toBeVisible();
});

test("Capture and AI provider: model install is guarded and credential entry uses the terminal", async ({ paired: page, hub }) => {
  await page.goto(`${hub.url}capture?type=audio`);
  const notice = page.getByText("Local speech model is not installed");
  if (await notice.count()) {
    await notice.click();
    await expect(page.getByTestId("model-install")).toBeDisabled(); // nothing downloads without a decision
    await page.getByTestId("model-confirm").check();
    await expect(page.getByTestId("model-install")).toBeEnabled();
    await page.getByTestId("model-confirm").uncheck();
  }
  await page.goto(`${hub.url}ai`);
  await page.getByText("Manage the credential").click();
  await expect(page.getByLabel("Credential value")).toHaveCount(0);
  await expect(page.getByTestId("credential-store")).toHaveCount(0);
  await expect(page.getByText(/terminal's hidden prompt/).first()).toBeVisible();
  expect(await serious(page)).toEqual([]);
});

test("Approvals: plan a purge, review it, commit exactly that plan", async ({ paired: page, hub, api }) => {
  const note = await api.createNote("E2E purge target", "This note will be purged.");
  await page.goto(`${hub.url}approvals`);
  await expect(page.getByTestId("retention")).toBeVisible();
  expect(await serious(page)).toEqual([]);
  await page.getByLabel(/Record IDs to purge/).fill(note);
  await page.getByTestId("retention-plan").click();
  const preview = page.getByTestId("retention-preview");
  await expect(preview).toContainText(note);
  await expect(preview).toContainText("cannot be undone");
  await expect(page.getByTestId("retention-commit")).toBeDisabled();
  expect((await api.record(note)).body.text).toBe("This note will be purged.");
  await page.getByTestId("retention-confirm").check();
  await page.getByTestId("retention-commit").click();
  await expect(page.getByTestId("retention-done")).toContainText("Purged 1 record");
  const history = await api.get<{ revisions: { content_purged: boolean; operation: string; lifecycle: string }[] }>(
    `/records/${note}/history`,
  );
  expect(history.revisions[0]!.content_purged).toBe(true); // the content is gone
  const last = history.revisions[history.revisions.length - 1]!;
  expect([last.operation, last.lifecycle]).toEqual(["purge", "deleted"]); // the deletion is recorded without it
});

test("Artifacts: check for unreferenced files", async ({ paired: page, hub }) => {
  await page.goto(`${hub.url}artifacts`);
  await page.getByTestId("orphans-check").click();
  await expect(page.getByTestId("orphans-result")).toContainText("unreferenced file");
  expect(await serious(page)).toEqual([]);
});

export type { Api };
