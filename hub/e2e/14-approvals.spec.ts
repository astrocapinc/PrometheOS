// CP10.2 owner review queue in the real hub (decision 0016). A local agent (bearer token, agent
// scope, no browser) submits two protected transactions and gets approval_required; the owner
// grants one and denies the other on the Approvals page; the agent resumes. Every step is
// verified by re-reading the stored state through a separately paired owner client: the granted
// plan executes exactly once, a second resume replays it, and the denied plan never runs.
import AxeBuilder from "@axe-core/playwright";
import { request as pwRequest, type APIRequestContext } from "@playwright/test";
import { expect, origin, test, type Api, type HubInfo } from "./fixtures";

type AgentHub = HubInfo & { agent_token: string };

function noteBody(title: string) {
  return {
    title,
    text: "Written by the agent through its own token.",
    category: "note",
    evidence: [],
    claim_refs: [],
    author_ref: null,
    origin: "agent",
    quantities: [],
    review: "unreviewed",
  };
}

async function agentContext(hub: AgentHub): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${hub.agent_token}` },
  });
}

async function agentSeq(agent: APIRequestContext): Promise<number> {
  const reply = await agent.get("/api/v1/project");
  expect(reply.status(), await reply.text()).toBe(200);
  return ((await reply.json()) as { project_seq: number }).project_seq;
}

/** Create a note (an ordinary write the agent may make). */
async function createNote(agent: APIRequestContext, hub: AgentHub, title: string): Promise<string> {
  const id = `entry_${crypto.randomUUID()}`;
  const created = await agent.post("/api/v1/transactions", {
    data: {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await agentSeq(agent),
      operations: [
        { op: "create", kind: "entry", id, base_rev: 0, sensitivity: "normal", occurred_at: null, body: noteBody(title) },
      ],
    },
  });
  expect(created.status(), await created.text()).toBe(200);
  return id;
}

/** Ask to restrict a note (a protected effect): approval_required, nothing committed. Both
 * requests are made at the same project sequence, so each still applies when decided. */
async function requestRestriction(agent: APIRequestContext, hub: AgentHub, id: string, title: string) {
  const asked = await agent.post("/api/v1/transactions", {
    data: {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await agentSeq(agent),
      operations: [
        { op: "revise", id, base_rev: 1, sensitivity: "restricted", occurred_at: null, body: noteBody(title) },
      ],
    },
  });
  expect(asked.status(), await asked.text()).toBe(403);
  const error = ((await asked.json()) as { error: { code: string; details: Record<string, any> } }).error;
  expect(error.code).toBe("approval_required");
  expect(error.details.owner_route.hub).toBe(`/approvals/${error.details.approval_request_id}`);
  return { id, approvalId: error.details.approval_request_id as string, digest: error.details.plan_sha256 as string };
}

async function approvalState(api: Api, approvalId: string): Promise<string> {
  return (await api.get<{ state: string }>(`/approvals/${approvalId}`)).state;
}

test("approvals queue: grant runs once, deny never runs, the agent never sees a secret", async ({
  paired: page,
  hub,
  api,
}) => {
  const agentHub = hub as AgentHub;
  expect(agentHub.agent_token, "hub_fixture provides an agent token").toBeTruthy();
  const agent = await agentContext(agentHub);
  try {
    // The agent token never reaches owner routes.
    const denied = await agent.get("/api/v1/changes?wait=0");
    expect(denied.status()).toBe(403);

    const grantNote = await createNote(agent, agentHub, "Approval e2e: to be granted");
    const denyNote = await createNote(agent, agentHub, "Approval e2e: to be denied");
    const granted = await requestRestriction(agent, agentHub, grantNote, "Approval e2e: to be granted");
    const refused = await requestRestriction(agent, agentHub, denyNote, "Approval e2e: to be denied");
    expect(await approvalState(api, granted.approvalId)).toBe("pending");

    // The owner sees both exact plans on the Approvals page and decides them.
    await page.goto(`${hub.url}approvals`);
    await expect(page.getByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
    // The sidebar counts pending requests; the link keeps its exact name and describes the count.
    const sideLink = page
      .getByRole("navigation", { name: "Project tools" })
      .getByRole("link", { name: "Approvals", exact: true });
    await expect(page.getByTestId("nav-count-approvals")).toHaveText(/^[1-9][0-9]*\+?$/);
    await expect(sideLink).toHaveAccessibleDescription(/waiting for you/);
    const grantCard = page.locator(`[data-testid="approval-pending"][data-approval-id="${granted.approvalId}"]`);
    const denyCard = page.locator(`[data-testid="approval-pending"][data-approval-id="${refused.approvalId}"]`);
    await expect(grantCard).toBeVisible();
    await expect(denyCard).toBeVisible();
    await expect(grantCard.getByTestId("approval-summary")).toContainText("raise sensitivity");
    // Round 2 (review F2): Grant stays disabled until the owner has opened the full plan.
    await expect(grantCard.getByTestId("approval-grant")).toBeDisabled();
    await grantCard.getByTestId("approval-review").click();
    await expect(grantCard.getByTestId("approval-preview")).toContainText("raise sensitivity");
    await expect(grantCard.getByTestId("approval-digest")).toHaveText(granted.digest);
    await expect(grantCard.getByTestId("approval-plan")).toContainText(granted.id);
    await denyCard.getByTestId("approval-review").click();
    await expect(denyCard.getByTestId("approval-plan")).toContainText(refused.id);

    // Review INT-CP10-I1: with two cards expanded, every region keeps a distinct name.
    const names = await page
      .locator('[role="region"], section[aria-label]')
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute("aria-label") ?? n.getAttribute("aria-labelledby") ?? ""));
    expect(names.length).toBeGreaterThanOrEqual(2);
    expect(new Set(names).size).toBe(names.length);

    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    expect(axe.violations.filter((v) => v.id === "landmark-unique")).toEqual([]);

    await grantCard.getByTestId("approval-grant").click();
    await expect(grantCard).toHaveCount(0);
    await denyCard.getByTestId("approval-deny").click();
    await expect(denyCard).toHaveCount(0);
    expect(await approvalState(api, granted.approvalId)).toBe("granted");
    expect(await approvalState(api, refused.approvalId)).toBe("denied");
    await expect(page.locator(`[data-testid="approval-granted"][data-approval-id="${granted.approvalId}"]`)).toBeVisible();

    // The agent's view of its granted request carries no secret and no capability ID.
    const view = await agent.get(`/api/v1/approvals/${granted.approvalId}`);
    const viewText = await view.text();
    expect(view.status()).toBe(200);
    expect(viewText).not.toContain("pcap_");
    expect(viewText).not.toContain("capability_id");

    // Resume: exactly once.
    const before = await api.seq();
    const first = await agent.post(`/api/v1/approvals/${granted.approvalId}/resume`, { data: {} });
    expect(first.status(), await first.text()).toBe(200);
    const firstBody = (await first.json()) as { replayed: boolean; result: { project_seq: number } };
    expect(firstBody.replayed).toBe(false);
    expect(await api.seq()).toBe(before + 1);
    expect((await api.get(`/records/${granted.id}`)).sensitivity).toBe("restricted");
    const second = await agent.post(`/api/v1/approvals/${granted.approvalId}/resume`, { data: {} });
    expect(second.status()).toBe(200);
    expect(((await second.json()) as { replayed: boolean }).replayed).toBe(true);
    expect(await api.seq()).toBe(before + 1);
    expect(await approvalState(api, granted.approvalId)).toBe("used");

    // The denied plan never runs.
    const blocked = await agent.post(`/api/v1/approvals/${refused.approvalId}/resume`, { data: {} });
    expect(blocked.status()).toBe(403);
    const blockedError = ((await blocked.json()) as { error: { code: string; details: { state: string } } }).error;
    expect(blockedError.code).toBe("policy_denied");
    expect(blockedError.details.state).toBe("denied");
    expect((await api.get(`/records/${refused.id}`)).sensitivity).toBe("normal");
    expect(await api.seq()).toBe(before + 1);

    // The queue's history shows both outcomes.
    await page.reload();
    const history = page.getByTestId("approval-history");
    await expect(history.locator(`[data-approval-id="${granted.approvalId}"]`)).toHaveAttribute("data-state", "used");
    await expect(history.locator(`[data-approval-id="${refused.approvalId}"]`)).toHaveAttribute("data-state", "denied");
  } finally {
    await agent.dispose();
  }
});
