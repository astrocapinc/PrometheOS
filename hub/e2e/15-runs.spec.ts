// CP10.3 operation runs in the real hub (decision 0017). A local agent (bearer token, agent
// scope) starts long operations over HTTP and gets an oprun_ ID at once; the owner follows them
// on the Runs page and resumes and cancels one there. Every step is checked against the stored
// state read back through a separately paired owner client (GET /api/v1/runs/{id}).
import AxeBuilder from "@axe-core/playwright";
import { request as pwRequest, type APIRequestContext } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, origin, test, type Api, type HubInfo } from "./fixtures";

type AgentHub = HubInfo & { agent_token: string };

interface Run {
  run_id: string;
  operation: string;
  state: string;
  attempts: number;
  cancel_confirmed: boolean;
  cancel_requested_at: string | null;
  resumable: boolean;
  result: Record<string, unknown> | null;
  artifacts: { id: string; rev: number; role: string }[];
  error: { code: string; message: string } | null;
}

const RUN_ID = /^oprun_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

async function agentContext(hub: AgentHub): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${hub.agent_token}` },
  });
}

async function settled(api: Api, runId: string): Promise<Run> {
  let run = await api.get<Run>(`/runs/${runId}`);
  for (let i = 0; i < 200 && (run.state === "queued" || run.state === "running"); i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    run = await api.get<Run>(`/runs/${runId}`);
  }
  return run;
}

test("runs: an agent's long operations answer a run at once; the owner follows, resumes and cancels them", async ({
  paired: page,
  hub,
  api,
}) => {
  const agentHub = hub as AgentHub;
  expect(agentHub.agent_token, "hub_fixture provides an agent token").toBeTruthy();
  const agent = await agentContext(agentHub);
  try {
    // 1. An import plan answers its run at once (no wait), then completes in the hub process.
    const name = `runs-e2e-${crypto.randomUUID().slice(0, 8)}.md`;
    writeFileSync(join(hub.seed.latex_folder, name), "A result written for the runs walk.\n", "utf-8");
    const started = await agent.post("/api/v1/sources/plans", {
      data: { root_alias: "research", paths: [`latex/${name}`], retention: "copy" },
    });
    expect(started.status(), await started.text()).toBe(200);
    const planRun = (await started.json()) as Run;
    expect(planRun.run_id).toMatch(RUN_ID);
    expect(["queued", "running", "completed"]).toContain(planRun.state);
    const planned = await settled(api, planRun.run_id);
    expect(planned.state, JSON.stringify(planned.error)).toBe("completed");
    const planId = String(planned.result?.plan_id);
    expect(planRun.run_id).toContain(planId);

    // The owner sees the same run, in the same state, on the Runs page.
    await page.goto(`${hub.url}runs/${planRun.run_id}`);
    await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
    const planCard = page.locator(`[data-testid="run-card"][data-run-id="${planRun.run_id}"]`);
    await expect(planCard).toHaveAttribute("data-run-state", "completed");
    await expect(planCard).toContainText("Import plan");
    await planCard.getByText("Logs, stages and result").click();
    await expect(planCard.getByTestId("run-result")).toContainText(planId);
    await expect(planCard.getByTestId("run-resume")).toHaveCount(0);

    // 2. The commit waits up to 20 s on the server and answers with its result.
    const committed = await agent.post(`/api/v1/sources/plans/${planId}/commit`, { data: { wait_seconds: 20 } });
    expect(committed.status(), await committed.text()).toBe(200);
    const commitRun = (await committed.json()) as Run;
    expect(commitRun.state).toBe("completed");
    const stored = await api.get<Run>(`/runs/${commitRun.run_id}`);
    expect(stored.artifacts.map((a) => a.role)).toContain("imported");
    expect(stored.result?.ok).toBe(true);

    // 3. A run that fails (the file does not exist) is resumable on the Runs page; resuming
    // retries it once more; cancelling it then is confirmed at once (nothing executes it).
    const failing = await agent.post("/api/v1/sources/plans", {
      data: { root_alias: "research", paths: ["latex/no-such-file.md"], retention: "copy" },
    });
    expect(failing.status(), await failing.text()).toBe(200);
    const failedRun = await settled(api, ((await failing.json()) as Run).run_id);
    expect(failedRun.state).toBe("failed");
    expect(failedRun.attempts).toBe(1);
    await page.goto(`${hub.url}runs`);
    const failedCard = page.locator(`[data-testid="run-card"][data-run-id="${failedRun.run_id}"]`);
    await expect(failedCard).toHaveAttribute("data-run-state", "failed");
    await expect(failedCard.getByTestId("run-error")).toContainText(failedRun.error?.code ?? "");
    // Two completed runs (the plan and the commit) with their results open.
    for (const runId of [planRun.run_id, commitRun.run_id]) {
      const card = page.locator(`[data-testid="run-card"][data-run-id="${runId}"]`);
      await card.getByText("Logs, stages and result").click();
      await expect(card.getByTestId("run-result")).toBeVisible();
    }

    // Review INT-CP10-I1: with two cards expanded, every region keeps a distinct name.
    const names = await page
      .locator('[role="region"], section[aria-label]')
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute("aria-label") ?? n.getAttribute("aria-labelledby") ?? ""));
    expect(names.length).toBeGreaterThanOrEqual(2);
    expect(new Set(names).size).toBe(names.length);

    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    expect(axe.violations.filter((v) => v.id === "landmark-unique")).toEqual([]);

    await failedCard.getByTestId("run-resume").click();
    await expect.poll(async () => (await api.get<Run>(`/runs/${failedRun.run_id}`)).attempts, { timeout: 20_000 }).toBe(2);
    expect((await settled(api, failedRun.run_id)).state).toBe("failed");
    await expect(failedCard).toHaveAttribute("data-run-state", "failed", { timeout: 10_000 });

    await failedCard.getByTestId("run-cancel").click();
    await expect(failedCard).toHaveAttribute("data-run-state", "cancelled", { timeout: 10_000 });
    const cancelled = await api.get<Run>(`/runs/${failedRun.run_id}`);
    expect(cancelled.state).toBe("cancelled");
    expect(cancelled.cancel_confirmed).toBe(true);
    await expect(failedCard.getByTestId("run-cancel")).toHaveCount(0);

    // The agent reads the same document through its own token.
    const agentView = await agent.get(`/api/v1/runs/${failedRun.run_id}`);
    expect(agentView.status()).toBe(200);
    expect(((await agentView.json()) as Run).state).toBe("cancelled");
  } finally {
    await agent.dispose();
  }
});
