// C17-WALK, hub half (tools/walk_cp17.py; spec/10 CP17.6). The owner's steps on the real built
// hub over a fresh store whose approved folder is the seeded synthetic corpus: scan it, review a
// family, make the consolidation plan; hand over to the agent's MCP session (it reports two plan
// steps and asks for a baseline); choose the family's current file and accept the baseline; hand
// over while the harness changes the folder; scan again and read the changes; hand over to the
// agent, which reads the drift from the baseline.
//
// The browser is Playwright's Chromium headless shell inside the loopback-only boundary of
// decision 0028. After every step the stored state is read back through a separately paired owner
// client, and each step is written to WALK_STEPS for the transcript the harness compares with the
// MCP half.
import { expect, request as pwRequest, type APIRequestContext, type Page } from "@playwright/test";
import { guardedTest as test } from "../e2e/boundary-guard";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

interface Info {
  url: string;
  project_id: string;
  pairing_tokens: string[];
}
interface Assertion {
  claim: string;
  passed: boolean;
  observed?: unknown;
}
interface Step {
  id: string;
  via: string;
  request: unknown;
  response: unknown;
  outcome: string;
  assertions: Assertion[];
}
interface Fixture {
  alias: string;
  family_key: string;
  choice: string;
  moved: string;
}

const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;
const fx = JSON.parse(process.env.WALK_FIXTURE ?? "{}") as Fixture;
const stepsPath = process.env.WALK_STEPS ?? "walk-steps.json";
const signals = process.env.WALK_SIGNALS ?? "walk-signals";
const shots = process.env.WALK_SHOTS ?? "walk-shots";
const origin = info.url.replace(/\/$/, "");
const steps: Step[] = [];
let tokenIndex = 0;

function check(list: Assertion[], claim: string, passed: boolean, observed?: unknown) {
  list.push({ claim, passed, observed });
}

function save() {
  writeFileSync(stepsPath, JSON.stringify(steps, null, 2), "utf-8");
}

async function pairedOwner(): Promise<APIRequestContext> {
  const ctx = await pwRequest.newContext({ baseURL: origin, extraHTTPHeaders: { Origin: origin } });
  const reply = await ctx.post("/api/v1/pair", { data: { token: info.pairing_tokens[tokenIndex++] } });
  expect(reply.status()).toBe(200);
  return ctx;
}

async function pairPage(page: Page): Promise<void> {
  const reply = await page.request.post(`${origin}/api/v1/pair`, {
    data: { token: info.pairing_tokens[tokenIndex++] },
    headers: { Origin: origin },
  });
  expect(reply.status()).toBe(200);
}

/** Hand over to the harness and wait for its answer. */
async function handOver(done: string, answer: string): Promise<void> {
  writeFileSync(join(signals, done), "done", "utf-8");
  const until = Date.now() + 420_000;
  while (Date.now() < until) {
    if (existsSync(join(signals, "harness.failed"))) {
      throw new Error(`the harness failed: ${readFileSync(join(signals, "harness.failed"), "utf-8")}`);
    }
    if (existsSync(join(signals, answer))) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`the harness did not answer ${answer} in time`);
}

test("C17-WALK: the owner's steps on the real hub", async ({ browser }) => {
  const owner = await pairedOwner();
  const ownerGet = async (path: string): Promise<any> => (await owner.get(`/api/v1${path}`)).json();
  const context = await browser.newContext();
  const page = await context.newPage();
  await pairPage(page);
  mkdirSync(shots, { recursive: true });
  const shot = async (name: string) => page.screenshot({ path: join(shots, `${name}.png`), fullPage: true });
  const record = (s: Step) => {
    steps.push(s);
    save();
  };
  const scanInHub = async (): Promise<any> => {
    await page.goto(`${origin}/adoption`);
    await expect(page.getByTestId("adoption-root")).toHaveValue(fx.alias);
    await page.getByTestId("adoption-scan").click();
    await expect(page.getByTestId("adoption-run-state")).toHaveAttribute("data-state", "completed", { timeout: 120_000 });
    return (await ownerGet("/adoption/scans")).scans[0];
  };
  // The page lists only the families that need the owner until asked for all (decision 0085).
  const showAllFamilies = async (): Promise<void> => {
    const toggle = page.getByTestId("adoption-show-all");
    if ((await toggle.textContent())?.startsWith("Show all")) await toggle.click();
  };
  const familyId = async (gen: number): Promise<string> =>
    ((await ownerGet(`/adoption/scans/${gen}/families?limit=500`)).families as any[]).find((f) => f.key === fx.family_key)
      ?.family_id ?? "";

  try {
    // ----- W1.1 the owner scans the folder ---------------------------------------------------
    let gen = 0;
    {
      const a: Assertion[] = [];
      const scan = await scanInHub();
      gen = scan.scan_gen;
      const families = await ownerGet(`/adoption/scans/${gen}/families?limit=500`);
      check(a, "the scan completed", scan.state === "complete", scan.state);
      check(a, "the folder was unchanged (root digests equal)", scan.root_digest_before === scan.root_digest_after);
      check(a, "families were found", families.total > 0, families.total);
      const asking = (families.families as any[]).filter((f) => f.needs_owner).length;
      await expect(page.getByTestId("adoption-family")).toHaveCount(asking);
      await showAllFamilies();
      await expect(page.getByTestId("adoption-family")).toHaveCount(families.total);
      await shot("W1.1-families");
      record({ id: "W1.1", via: "browser", request: { root_alias: fx.alias }, response: scan, outcome: "ok", assertions: a });
    }
    // ----- W1.2 the owner reviews a family -------------------------------------------------
    {
      const a: Assertion[] = [];
      const id = await familyId(gen);
      await page.locator(`[data-testid="adoption-family"][data-family-id="${id}"]`).getByTestId("adoption-family-open").click();
      const family = await ownerGet(`/adoption/scans/${gen}/families/${id}`);
      const detail = page.getByTestId("adoption-family-detail");
      await expect(detail.getByTestId("adoption-family-member")).toHaveCount(family.members.length);
      check(a, "the family has members and reasons", family.members.length > 1 && family.members.every((m: any) => "reasons" in m), family.members.length);
      await shot("W1.2-family");
      record({ id: "W1.2", via: "browser", request: { key: fx.family_key }, response: family, outcome: "ok", assertions: a });
    }
    // ----- W1.3 the owner makes the plan ---------------------------------------------------
    {
      const a: Assertion[] = [];
      const section = page.getByTestId("adoption-plan-section");
      await section.getByTestId("adoption-plan-create").click();
      await expect(section.getByTestId("adoption-plan-create")).toHaveText("Update the plan");
      const plan = await ownerGet(`/adoption/scans/${gen}/plan`);
      const live = (plan.steps as any[]).filter((s) => s.state !== "superseded");
      await expect(section.getByTestId("adoption-plan-step")).toHaveCount(live.length);
      const items = (await ownerGet("/records?kind=action&limit=200")).items as any[];
      check(a, "the plan has steps", live.length > 0, live.length);
      check(a, "each step is a proposed work item", items.length === live.length && items.every((r) => r.body.status === "proposed"), items.length);
      await shot("W1.3-plan");
      record({ id: "W1.3", via: "browser", request: { scan_gen: gen }, response: { plan_id: plan.plan_id, version: plan.version, steps: live.length, work_items_created: items.length, scan_gen: gen }, outcome: "ok", assertions: a });
    }
    await handOver("owner-1.done", "agent-1.done");

    // ----- W3.1 the owner chooses the family's current file ----------------------------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/adoption`);
      await showAllFamilies();
      const id = await familyId(gen);
      await page.locator(`[data-testid="adoption-family"][data-family-id="${id}"]`).getByTestId("adoption-family-open").click();
      const detail = page.getByTestId("adoption-family-detail");
      const member = detail.locator(`[data-testid="adoption-family-member"][data-path="${fx.choice}"]`);
      await member.getByTestId("adoption-decide").click();
      await expect(member).toContainText("chosen");
      const family = await ownerGet(`/adoption/scans/${gen}/families/${id}`);
      check(a, "the chosen file is current by the owner's rule (R1)", family.current === fx.choice && family.deciding_rule === "R1", [family.current, family.deciding_rule]);
      record({ id: "W3.1", via: "browser", request: { family: fx.family_key, current: fx.choice }, response: family, outcome: "ok", assertions: a });
    }
    // ----- W3.2 the owner accepts the baseline -----------------------------------------------
    {
      const a: Assertion[] = [];
      const section = page.getByTestId("adoption-plan-section");
      await section.getByTestId("adoption-baseline-accept").click();
      await expect(section.getByTestId("adoption-baseline")).toBeVisible();
      const baselines = (await ownerGet("/adoption/baselines")).baselines as any[];
      const plan = await ownerGet(`/adoption/scans/${gen}/plan`);
      const states = (plan.steps as any[])
        .filter((s) => ["dedupe_proposal", "convention_proposal"].includes(s.step.kind))
        .map((s) => s.state);
      check(a, "a baseline was accepted", baselines.length === 1, baselines.length);
      check(a, "the agent's validated results were accepted with it", JSON.stringify(states) === JSON.stringify(["accepted", "accepted"]), states);
      await shot("W3.2-baseline");
      record({ id: "W3.2", via: "browser", request: { scan_gen: gen }, response: baselines[0] ?? {}, outcome: baselines.length ? "ok" : "error", assertions: a });
    }
    await handOver("owner-2.done", "folder.done");

    // ----- W5.1 the owner scans again and reads the changes ----------------------------------
    {
      const scan = await scanInHub();
      const drift = await ownerGet(`/adoption/scans/${scan.scan_gen}/drift`);
      const a: Assertion[] = [];
      check(a, "the rescan completed without writing", scan.state === "complete" && scan.root_digest_before === scan.root_digest_after);
      check(a, "one edit, one move, one deletion, one addition", ["added", "changed", "moved", "deleted"].every((k) => drift.counts[k] === 1) && Object.keys(drift.counts).length === 4, drift.counts);
      check(a, "only what changed was analysed again", drift.analysis.extracted <= 3, drift.analysis);
      const changes = page.getByTestId("adoption-changes-section");
      await expect(changes.locator('[data-against="previous"] [data-testid="adoption-drift-counts"]')).toContainText("1 added, 1 changed, 1 moved, 1 deleted");
      await expect(changes.locator('[data-against="baseline"] [data-testid="adoption-drift-family"][data-change="moved"]')).toContainText(fx.moved);
      await shot("W5.1-changes");
      record({ id: "W5.1", via: "browser", request: { root_alias: fx.alias }, response: drift, outcome: "ok", assertions: a });
    }
    await handOver("owner-3.done", "agent-2.done");
  } finally {
    save();
  }
});
