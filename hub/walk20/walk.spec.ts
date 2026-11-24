// C20-WALK, the hub leg (CP20.5; decisions 0106, 0107). The owner's steps in the hub for a fresh
// home whose research folder is the synthetic forking repository (tools/research_map_fixture.py);
// the harness (tools/walk_cp20.py) runs the agent's MCP session between them and reads every
// outcome back from the store.
//
//   S1 scan the folder, read the outline, correct it (RQ1 under the paper, the clutch question
//      nowhere) and confirm it once
//   (harness: S2 the agent records the claims, the pair, its assessments and the correction)
//   S3 the research map: each claim's status, basis and causes under RQ1; the paper's stage
//   S4 open the stale claim's Details: its cause and verifiables
//   (harness: S5 the agent starts the eligible check; S6 the rerun and two carded requests)
//   S7 the second card's default in effect; grant the first card; after the agent resumed it,
//      the corrected claim and its check with expected, measured and tolerance
import { expect, type Locator, type Page } from "@playwright/test";
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

const PAPER = "Kinematic limits of a flywheel KERS";
const RQ1 = "Does a flywheel KERS cut the energy use of a city bus on the urban cycle?";
const CLUTCH = "What limits the regenerative torque of the clutch?";
const TRACK5 = "Track 5 (hilly route) uses 238.0 kWh.";
const OLD = "The bus saves 18.2 % fuel.";
const NEW = "The bus saves 17.4 % fuel.";
const CARD = "Replace the 18.2 % fuel saving with 17.4 %";
const SECOND = "Withdraw the track 5 energy until its rerun";

async function titles(section: Locator, kind: string): Promise<string[]> {
  return section.locator(`[data-testid="outline-item"][data-kind="${kind}"]`).evaluateAll((els) =>
    els.map((el) => {
      const input = el.querySelector('[data-testid="outline-title"]') as HTMLInputElement | null;
      return (input ? input.value : el.querySelector(".card-title")?.textContent ?? "").trim();
    }),
  );
}

async function openPaper(page: Page): Promise<Locator> {
  await page.goto(info.url);
  const card = page.getByTestId("deliverable").filter({ hasText: PAPER });
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.getByRole("link", { name: PAPER }).click();
  const map = page.getByTestId("paper-map");
  await expect(map).toBeVisible({ timeout: 30_000 });
  return map;
}

/** Each question of the paper with its claims: the line, the derived status and its words. */
async function questionsOf(map: Locator) {
  return map.getByTestId("map-question").evaluateAll((els) =>
    els.map((el) => ({
      title: (el.querySelector("h3")?.childNodes[0]?.textContent ?? "").trim(),
      claims: [...el.querySelectorAll('[data-testid="map-claim"]')].map((c) => ({
        line: (c.querySelector(".map-claim-line")?.childNodes[0]?.textContent ?? "").trim(),
        status: c.getAttribute("data-status"),
        text: (c.querySelector('[data-testid="claim-status"]')?.textContent ?? "").trim(),
      })),
    })),
  );
}

async function details(map: Locator, line: string): Promise<Locator> {
  const row = map.getByTestId("map-claim").filter({ hasText: line });
  await expect(row).toHaveCount(1);
  await row.locator("summary", { hasText: "Details" }).click();
  const shown = row.getByTestId("claim-details");
  await expect(shown).toBeVisible();
  return shown;
}

test("C20-WALK hub leg", async ({ page }) => {
  signal("ready");
  await page.goto(info.pair_url);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible({ timeout: 30_000 });

  // S1: scan, read the outline, correct it and confirm it once.
  await waitFor("adopt");
  await page.goto(new URL("/adoption", info.url).toString());
  await page.getByTestId("adoption-scan").click();
  const section = page.getByTestId("adoption-outline-section");
  await expect(section.locator('[data-testid="outline-item"][data-kind="stale_candidate"]')).toBeVisible({
    timeout: 60_000,
  });
  const question = (title: string) =>
    section.locator('[data-testid="outline-item"][data-kind="question"]').filter({ hasText: title });
  const stale = (await section.locator('[data-kind="stale_candidate"] [data-testid="outline-result"]').allTextContents()).map(
    (t) => t.trim(),
  );
  await question(RQ1).getByTestId("outline-place").selectOption({ label: PAPER });
  await question(CLUTCH).getByTestId("outline-place").selectOption({ label: "Nowhere yet" });
  const counts = section.getByTestId("outline-counts");
  await expect(counts).toContainText("placed nowhere", { timeout: 15_000 });
  const countsText = ((await counts.textContent()) ?? "").trim();
  const shown = {
    tracks: await titles(section, "track"),
    deliverables: await titles(section, "deliverable"),
    questions: await titles(section, "question"),
    stale,
    counts: countsText,
  };
  await shot(page, "S1-outline-corrected");
  await expect(section.getByTestId("outline-confirm")).toBeEnabled();
  await section.getByTestId("outline-confirm").click();
  await expect(section.getByTestId("outline-confirmed")).toContainText("Confirmed", { timeout: 15_000 });
  const buttons = await section.getByTestId("outline-confirm").count();
  await shot(page, "S1-outline-confirmed");
  record({ id: "S1", ...shown, confirmed: true, confirm_buttons: buttons });
  signal("adopted");

  // S3: the research map, each claim's status, basis and causes under its question.
  await waitFor("map");
  await page.goto(info.url);
  const card = page.getByTestId("deliverable").filter({ hasText: PAPER });
  await expect(card).toBeVisible({ timeout: 30_000 });
  const stage = ((await card.getByTestId("stage-summary").textContent()) ?? "").trim();
  const health = ((await card.getByTestId("claim-health").textContent()) ?? "").trim();
  await shot(page, "S3-now-deliverable");
  const map = await openPaper(page);
  await expect(map.getByTestId("map-claim")).toHaveCount(5);
  const questions = await questionsOf(map);
  const blockers = (await map.getByTestId("stage-next").locator("li").allTextContents()).map((t) => t.trim());
  const toConfirm = (await map.getByTestId("to-confirm").locator("li").allTextContents()).map((t) => t.trim());
  await shot(page, "S3-paper-map");
  record({ id: "S3", stage, health, questions, blockers, to_confirm: toConfirm });

  // S4: open the stale claim's cause and verifiables.
  const stalest = await details(map, TRACK5);
  const why = await stalest.evaluate((dl) => {
    const dt = [...dl.querySelectorAll("dt")].find((d) => d.textContent?.trim() === "Why");
    return [...(dt?.nextElementSibling?.querySelectorAll("li") ?? [])].map((li) => li.textContent?.trim() ?? "");
  });
  const evidence = stalest.getByTestId("claim-evidence").first();
  const evidenceText = ((await evidence.textContent()) ?? "").trim();
  const locator = await evidence.getAttribute("data-locator");
  await shot(page, "S4-stale-claim-details");
  record({ id: "S4", why, evidence: evidenceText, locator });
  signal("map-seen");

  // S7: the second card's default has passed and is in effect in views; then the owner answers
  // the first card by granting exactly its plan.
  const { default_at: due } = (await waitFor("cards")) as { default_at: string };
  while (Date.now() < Date.parse(due) + 1500) await page.waitForTimeout(500);
  await page.goto(info.url);
  const cards = page.getByTestId("decision-cards");
  const first = cards.getByTestId("decision-card").filter({ hasText: CARD });
  const second = cards.getByTestId("decision-card").filter({ hasText: SECOND });
  await expect(second.getByTestId("card-default")).toContainText("in effect in views since", { timeout: 15_000 });
  const secondDefault = ((await second.getByTestId("card-default").textContent()) ?? "").trim();
  const firstDefault = ((await first.getByTestId("card-default").textContent()) ?? "").trim();
  await shot(page, "S7-default-in-effect");
  signal("default-seen");
  await waitFor("grant");
  const review = first.getByRole("link", { name: "Review this one" });
  const id = ((await review.getAttribute("href")) ?? "").split("/").pop() ?? "";
  await review.click();
  const pending = page.locator(`[data-testid="approval-pending"][data-approval-id="${id}"]`);
  await expect(pending).toBeVisible({ timeout: 15_000 });
  // A deep link reviews the full plan by itself; the button is only for a card opened in the queue.
  const detail = pending.getByTestId("approval-detail");
  const reviewed = await detail
    .waitFor({ state: "visible", timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (!reviewed) await pending.getByTestId("approval-review").click();
  await expect(detail).toBeVisible({ timeout: 15_000 });
  await shot(page, "S7-review-card");
  await pending.getByTestId("approval-grant").click();
  await expect(page.locator(`[data-testid="approval-granted"][data-approval-id="${id}"]`)).toBeVisible({
    timeout: 15_000,
  });
  signal("granted");

  // After the agent resumed the granted plan: the cards left and the map's statuses.
  await waitFor("resumed");
  await page.goto(info.url);
  await expect(cards.getByTestId("decision-card")).toHaveCount(1, { timeout: 15_000 });
  const cardsAfter = (await cards.getByTestId("decision-card").locator(".card-title").allTextContents()).map((t) =>
    t.trim(),
  );
  const after = await openPaper(page);
  const rows = (await questionsOf(after)).flatMap((q) => q.claims);
  const statusOf = (line: string) => rows.find((r) => r.line === line)?.status ?? null;
  const checked = await details(after, NEW);
  const checks = ((await checked.getByTestId("claim-checks").textContent()) ?? "").trim();
  await shot(page, "S7-after-resume");
  record({
    id: "S7",
    second_default: secondDefault,
    first_default: firstDefault,
    granted: true,
    cards_after: cardsAfter,
    after: { saving_old: statusOf(OLD), saving_new: statusOf(NEW), track5: statusOf(TRACK5) },
    checks,
  });
  signal("after-seen");
  await waitFor("done", 300);
});
