// CP10.5 recovery screen in the real hub (S27; decision 0021). The fixture starts a second hub
// on a disposable project whose record.sqlite was corrupted after a verified backup was made;
// that hub chose maintenance mode by itself. Opening the project fails with a recovery pointer;
// the owner inspects, verifies the backup and restores it into a new store on the recovery
// screen; a local agent (bearer token, no browser) does the same over HTTP and asks to switch
// the registry, which answers approval_required; the owner grants it on the recovery screen and
// the agent resumes it once. Every step is checked by re-reading the stored registry state
// through a separately paired owner client: the registry switches only after the grant, the
// damaged store is unchanged (same database digest) and stays listed as quarantined.
import AxeBuilder from "@axe-core/playwright";
import { request as pwRequest, type APIRequestContext, type Page } from "@playwright/test";
import { expect, readHub, test } from "./fixtures";

interface RecoveryInfo {
  url: string;
  project_id: string;
  backup_name: string;
  damaged_db_sha256: string;
  project_seq: number;
  pairing_tokens: string[];
  agent_token: string;
}

function recoveryInfo(): RecoveryInfo {
  const info = readHub() as unknown as { recovery?: RecoveryInfo };
  if (!info.recovery) throw new Error("the hub fixture has no recovery hub (tools/hub_fixture.py)");
  return info.recovery;
}

const origin = (r: RecoveryInfo) => r.url.replace(/\/$/, "");

async function ownerClient(r: RecoveryInfo, token: string): Promise<{ ctx: APIRequestContext; csrf: string }> {
  const ctx = await pwRequest.newContext({ baseURL: origin(r), extraHTTPHeaders: { Origin: origin(r) } });
  const reply = await ctx.post("/api/v1/pair", { data: { token } });
  expect(reply.status(), await reply.text()).toBe(200);
  return { ctx, csrf: ((await reply.json()) as { csrf_token: string }).csrf_token };
}

async function agentClient(r: RecoveryInfo): Promise<APIRequestContext> {
  return pwRequest.newContext({ baseURL: origin(r), extraHTTPHeaders: { Authorization: `Bearer ${r.agent_token}` } });
}

async function pairPage(page: Page, r: RecoveryInfo, token: string): Promise<void> {
  const reply = await page.request.post(`${origin(r)}/api/v1/pair`, { data: { token }, headers: { Origin: origin(r) } });
  expect(reply.status(), await reply.text()).toBe(200);
}

test("S27: recover a damaged store on the recovery screen with an agent's switch granted by the owner", async ({ page }) => {
  const r = recoveryInfo();
  const owner = await ownerClient(r, r.pairing_tokens[1]);
  const agent = await agentClient(r);
  try {
    // Opening the project fails with a recovery pointer (HTTP), and the stored registry still
    // names the damaged store.
    const project = await agent.get("/api/v1/project");
    expect(project.status()).toBe(503);
    const pointer = ((await project.json()) as { error: { details: { recovery: { hub: string } } } }).error.details.recovery;
    expect(pointer.hub).toBe("/recovery");
    const first = (await (await owner.ctx.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(first.health).toBe("corrupt");
    expect(first.database.sha256).toBe(r.damaged_db_sha256);
    expect(first.quarantine).toEqual([]);
    const damagedLabel: string = first.store.label;
    expect(JSON.stringify(first)).not.toMatch(/[A-Za-z]:\\\\/); // no local path reaches the browser

    // The app shows its recovery screen before any destination.
    await pairPage(page, r, r.pairing_tokens[0]);
    await page.goto(`${origin(r)}/now`);
    await expect(page.getByRole("heading", { level: 1, name: "Recovery" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
    await expect(page.getByTestId("recovery-health")).toHaveAttribute("data-health", "corrupt");
    await expect(page.getByTestId("recovery-store-label")).toHaveText(damagedLabel);
    await expect(page.getByTestId("recovery-environment")).toBeAttached(); // doctor, inside a disclosure

    // The owner verifies the backup and restores it into a new store (no switch yet).
    const row = page.getByTestId("recovery-backup").filter({ hasText: r.backup_name });
    await expect(row.getByTestId("recovery-restore")).toBeDisabled();
    await row.getByTestId("recovery-verify").click();
    await expect(row.getByTestId("recovery-verified")).toContainText(`project sequence ${r.project_seq}`);
    await row.getByTestId("recovery-restore").click();
    await expect(page.getByTestId("recovery-restored")).toBeVisible();
    const ownerTarget = (await page.getByTestId("recovery-target-label").textContent()) ?? "";
    expect(ownerTarget).toMatch(/^<PrometheOS home>\/stores\//);
    let state = (await (await owner.ctx.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(state.store.label).toBe(damagedLabel); // a restore never switches the registry

    // The agent inspects, verifies and restores over HTTP, then asks to switch.
    const inspected = (await (await agent.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(inspected.backups.map((b: { name: string }) => b.name)).toContain(r.backup_name);
    expect((await agent.post("/api/v1/maintenance/verify", { data: { backup: r.backup_name } })).status()).toBe(200);
    const restored = await agent.post("/api/v1/maintenance/restore", { data: { backup: r.backup_name } });
    expect(restored.status(), await restored.text()).toBe(200);
    const agentTarget: string = ((await restored.json()) as { target_store_label: string }).target_store_label;
    const asked = await agent.post("/api/v1/maintenance/switch", { data: { backup: r.backup_name, target: agentTarget } });
    expect(asked.status()).toBe(403);
    const details = ((await asked.json()) as { error: { code: string; details: any } }).error;
    expect(details.code).toBe("approval_required");
    const approvalId: string = details.details.approval_request_id;
    expect(approvalId).toMatch(/^mapreq_/);
    expect(details.details.owner_route.hub).toBe("/recovery");
    // Held in the per-user registry, bound to the store, backup and target; nothing switched.
    state = (await (await owner.ctx.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(state.store.label).toBe(damagedLabel);
    const held = state.approvals.find((a: any) => a.approval_request_id === approvalId);
    expect(held.state).toBe("pending");
    expect(held.damaged_db_sha256).toBe(r.damaged_db_sha256);
    expect(held.target_store_label).toBe(agentTarget);
    expect(held.backup_manifest_sha256).toMatch(/^[0-9a-f]{64}$/);
    // The agent token cannot grant it.
    const forged = await agent.post(`/api/v1/maintenance/approvals/${approvalId}/grant`, {
      data: { plan_sha256: held.plan_sha256 },
    });
    expect(forged.status()).toBe(403);

    // The owner grants it on the recovery screen (the page polls for new requests).
    const card = page.locator(`[data-testid="recovery-request"][data-approval-id="${approvalId}"]`);
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByTestId("recovery-request-digest")).toHaveText(held.plan_sha256);
    await expect(card.getByTestId("recovery-request-summary")).toContainText(agentTarget);
    await expect(card.getByTestId("recovery-request-target")).toHaveText(agentTarget);
    await expect(card.getByTestId("recovery-request-health")).toContainText("corrupt");
    // An agent cannot name a location for its restore (round 2, F1).
    const elsewhere = await agent.post("/api/v1/maintenance/restore", {
      data: { backup: r.backup_name, into: "<PrometheOS home>/agent-pick" },
    });
    expect(elsewhere.status()).toBe(403);
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    await card.getByTestId("recovery-grant").click();
    await expect(card).toHaveAttribute("data-state", "granted");
    state = (await (await owner.ctx.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(state.store.label).toBe(damagedLabel); // a grant alone switches nothing

    // The agent resumes it once: the registry switches, after verification, in one step.
    const resumed = await agent.post(`/api/v1/maintenance/approvals/${approvalId}/resume`);
    expect(resumed.status(), await resumed.text()).toBe(200);
    expect(((await resumed.json()) as { replayed: boolean }).replayed).toBe(false);
    const replay = await agent.post(`/api/v1/maintenance/approvals/${approvalId}/resume`);
    expect(((await replay.json()) as { replayed: boolean }).replayed).toBe(true);

    state = (await (await owner.ctx.get("/api/v1/maintenance/inspect")).json()) as any;
    expect(state.store.label).toBe(agentTarget);
    expect(state.health).toBe("ok");
    expect(state.approvals.find((a: any) => a.approval_request_id === approvalId).state).toBe("used");
    expect(state.quarantine).toHaveLength(1);
    const record = state.quarantine[0];
    expect(record.maintenance_approval_id).toBe(approvalId);
    expect(record.damaged_store_label).toBe(damagedLabel);
    expect(record.damaged_integrity).toBe("corrupt");
    expect(record.damaged_db_sha256).toBe(r.damaged_db_sha256);
    // The damaged store is unchanged and can still be inspected.
    const kept = (await (await owner.ctx.get(`/api/v1/maintenance/inspect?quarantine_id=${record.id}`)).json()) as any;
    expect(kept.store.kind).toBe("quarantined");
    expect(kept.database.sha256).toBe(r.damaged_db_sha256);
    expect(kept.health).toBe("corrupt");

    // The screen lists it for inspection and says to restart the hub (it is still in maintenance
    // mode, while the registered store now opens).
    await expect(page.getByTestId("recovery-restart")).toBeVisible();
    const listed = page.getByTestId("recovery-quarantined");
    await expect(listed).toHaveCount(1);
    await expect(listed).toHaveAttribute("data-integrity", "corrupt");
    await listed.getByTestId("recovery-quarantine-inspect").click();
    await expect(page.getByTestId("recovery-quarantine-health")).toContainText("Damaged");
  } finally {
    await owner.ctx.dispose();
    await agent.dispose();
  }
});

test("the recovery screen is reachable from the normal hub and lists maintenance requests", async ({ paired, hub }) => {
  await paired.goto(hub.url);
  await paired.getByTestId("header-recovery").click();
  await expect(paired.getByRole("heading", { level: 1, name: "Recovery" })).toBeVisible();
  await expect(paired.getByTestId("recovery-health")).toHaveAttribute("data-health", "ok");
  await expect(paired.getByTestId("recovery-owner-actions")).toBeVisible(); // backup button
  await paired.getByTestId("recovery-refresh").click();
  await expect(paired.getByTestId("recovery-health")).toHaveAttribute("data-health", "ok");
});
