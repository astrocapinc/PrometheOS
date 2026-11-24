// C22-WALK, the hub leg (CP22.6; decision 0099). The owner's steps on the hub's AI page; the
// harness (tools/walk_cp22.py) runs the owner's terminal acts and the agent's MCP sessions
// between them and checks the store. No model is called: the hub's provider transport replays
// recorded replies behind the explicit test switch.
//
//   K1 the AI page and adoption with no key: the layer is off and each says what runs instead
//   (harness: K2 a run with no provider, K3 the owner's terminal acts, K4-K6 the agent's steps)
//   K7 adoption shows the proposals tagged; use the loss note's proposal as evidence
//   K8 revoke the standing policy
//   (harness: K9 a run after the revocation)
import { expect, type Page } from "@playwright/test";
import { guardedTest as test } from "../e2e/boundary-guard";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

type Info = { url: string; pair_url: string; signals: string; shots: string; steps: string };
const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;

function record(step: Record<string, unknown>): void {
  appendFileSync(info.steps, JSON.stringify(step) + "\n");
}

function signal(name: string, data: unknown = {}): void {
  const temp = join(info.signals, `${name}.tmp`);
  writeFileSync(temp, JSON.stringify(data));
  renameSync(temp, join(info.signals, `${name}.json`));
}

async function waitFor(name: string, seconds = 300): Promise<any> {
  const path = join(info.signals, `${name}.json`);
  const until = Date.now() + seconds * 1000;
  while (Date.now() < until) {
    if (existsSync(path)) return JSON.parse(readFileSync(path, "utf-8"));
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`the harness did not send ${name} in time`);
}

async function shot(page: Page, name: string): Promise<void> {
  mkdirSync(info.shots, { recursive: true });
  await page.screenshot({ path: join(info.shots, `${name}.png`), fullPage: true });
}

const LOSS = "notes/loss-notes.md";
const RECORDED = "recorded reply (no call, no trust)";

async function adoptionLayer(page: Page) {
  await page.goto(new URL("/adoption", info.url).toString());
  const layer = page.getByTestId("adoption-ai");
  await expect(layer.getByTestId("adoption-ai-fallback")).toBeVisible({ timeout: 30_000 });
  return layer;
}

async function aiPage(page: Page): Promise<void> {
  await page.goto(new URL("/ai", info.url).toString());
  await expect(page.getByTestId("ai-layer")).toBeVisible({ timeout: 30_000 });
}

test("C22-WALK hub leg", async ({ page }) => {
  signal("ready");
  await page.goto(info.pair_url);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible({ timeout: 30_000 });

  // K1: no key, no policy: the layer is off and the page says what runs instead.
  await waitFor("open-off");
  await aiPage(page);
  const off = page.getByTestId("ai-policy-off");
  await expect(off).toBeVisible();
  const text = (await off.textContent()) ?? "";
  const restricted = await page.getByTestId("ai-root-sensitivity").locator("td", { hasText: /^restricted$/ }).count();
  await shot(page, "K1-off");
  const layer = await adoptionLayer(page);
  const fallback = layer.getByTestId("adoption-ai-fallback");
  await expect(fallback).toHaveAttribute("data-layer", "off");
  const line = (await fallback.textContent()) ?? "";
  await shot(page, "K1-adoption-off");
  record({
    id: "K1",
    off: text.startsWith("Off."),
    says_instead: text.includes("runs its rules"),
    restricted_roots: restricted,
    adoption_off: line.includes("is off"),
    adoption_instead: line.includes("rules") && line.includes("coding agent"),
  });
  signal("seen-off");

  // K7: the proposals show their source; the owner uses one as evidence.
  await waitFor("use-evidence");
  const onAdoption = await adoptionLayer(page);
  await expect(onAdoption.getByTestId("adoption-ai-fallback")).toHaveAttribute("data-layer", "on");
  const proposals = onAdoption.getByTestId("adoption-ai-proposal");
  await expect(proposals).toHaveCount(2);
  const adoptionTagged = (await proposals.evaluateAll((els) => els.map((e) => e.getAttribute("data-source")))).every(
    (source) => source === "cassette",
  );
  await shot(page, "K7-adoption-proposals");
  await aiPage(page);
  const results = page.getByTestId("ai-results");
  await expect(results).toBeVisible();
  // The clicked row: the loss note's first sample (its button sends the whole sample set).
  const row = results.locator("tr", { has: page.getByTestId("ai-accept") }).filter({ hasText: LOSS });
  await expect(row).toHaveCount(1);
  const rowSource = (await row.getByTestId("ai-result-source").textContent()) ?? "";
  await row.getByTestId("ai-accept").click();
  // After the click the row has no button any more: find it by its note and its new state.
  await expect(results.locator("tr").filter({ hasText: LOSS }).getByTestId("ai-accepted")).toBeVisible({
    timeout: 15_000,
  });
  await shot(page, "K7-used-as-evidence");
  record({
    id: "K7",
    adoption_tagged: adoptionTagged,
    tagged: rowSource === RECORDED,
    used: true,
  });
  signal("used");

  // K8: revoke the standing policy in the hub.
  await waitFor("revoke");
  await aiPage(page);
  await page.getByTestId("ai-policy-revoke").getByRole("button", { name: "Revoke the standing policy…" }).click();
  await page.getByTestId("ai-policy-revoke").getByRole("button", { name: "Revoke the policy" }).click();
  await expect(page.getByTestId("ai-policy-off")).toBeVisible({ timeout: 15_000 });
  await shot(page, "K8-revoked");
  record({ id: "K8", off: ((await page.getByTestId("ai-policy-off").textContent()) ?? "").startsWith("Off.") });
  signal("revoked");
  await waitFor("done", 300);
});
