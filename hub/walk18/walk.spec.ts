// C18-WALK, hub half (tools/walk_cp18.py; spec/10 CP18.4). The owner's steps on the real built
// hub over a fresh store: four work items added on the Work page (three with deadlines, one
// without), the checks run from Now's inbox and the deadline events appear there. Then the test
// hands over (WALK_SIGNALS/owner-1.done) to the agent's session, which the harness runs over the
// real MCP server while this hub keeps serving, and waits for it (agent.done). Afterwards the
// owner sees the accepted item done and acknowledges its deadline event with a note.
//
// The browser is Playwright's Chromium headless shell inside the loopback-only boundary of
// decision 0028. After every step the stored state is read back through a separately paired owner
// client, and each step is written to WALK_STEPS (id, request, response, outcome and store
// assertions) for the transcript the harness assembles and compares with the MCP half.
import { expect, request as pwRequest, type APIRequestContext, type Page } from "@playwright/test";
// The guarded `test`: the walk's browser worker is checked against the boundary before it is used.
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
  created?: Record<string, string>;
}
interface Item {
  label: string;
  title: string;
  reason: string;
  date: string;
  time: string;
  due_at: string | null;
  description: string | null;
  labels: string[];
  threshold: number | null;
}
interface Fixture {
  zone: string;
  items: Item[];
  accepted: string;
  ack_note: string;
}

const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;
const fx = JSON.parse(process.env.WALK_FIXTURE ?? "{}") as Fixture;
const stepsPath = process.env.WALK_STEPS ?? "walk-steps.json";
const signals = process.env.WALK_SIGNALS ?? "walk-signals";
const shots = process.env.WALK_SHOTS ?? "walk-shots";
const origin = info.url.replace(/\/$/, "");
const BROWSER_ZONE = "America/Los_Angeles"; // never the project's zone (CP18-F1)
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

/** Wait for the harness: the agent's session ran (or failed) while this hub kept serving. */
async function waitForAgent(): Promise<void> {
  const until = Date.now() + 420_000;
  while (Date.now() < until) {
    if (existsSync(join(signals, "agent.failed"))) {
      throw new Error(`the agent's session failed: ${readFileSync(join(signals, "agent.failed"), "utf-8")}`);
    }
    if (existsSync(join(signals, "agent.done"))) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("the agent's session did not finish in time");
}

test("C18-WALK: the owner's steps on the real hub", async ({ browser }) => {
  const owner = await pairedOwner();
  const ownerGet = async (path: string): Promise<any> => (await owner.get(`/api/v1${path}`)).json();
  // The browser runs in another zone than the project (CP18-F1; decision 0048): Los Angeles is
  // ten or eleven hours from Riyadh, so a deadline typed, stored or shown in the browser's zone
  // instead of the project's fails the walk's stored-UTC and displayed-zone checks.
  const context = await browser.newContext({ timezoneId: BROWSER_ZONE });
  const page = await context.newPage();
  await pairPage(page);
  mkdirSync(shots, { recursive: true });
  const shot = async (name: string) => page.screenshot({ path: join(shots, `${name}.png`), fullPage: true });
  const record = (s: Step) => {
    steps.push(s);
    save();
  };
  const ids: Record<string, string> = {};
  const zoneText = `(${fx.zone}, UTC+03:00)`;

  try {
    // ----- W1: the owner adds four work items on the Work page ------------------------------
    for (const [n, item] of fx.items.entries()) {
      const a: Assertion[] = [];
      await page.goto(`${origin}/work`);
      const form = page.getByTestId("add-work-item");
      await form.getByLabel("Title").fill(item.title);
      await form.getByLabel("Why it matters").fill(item.reason);
      if (item.date) {
        await form.getByLabel("Date").fill(item.date);
        await form.getByLabel("Time").fill(item.time);
      }
      if (item.description) await form.getByLabel(/What is wanted/).fill(item.description);
      if (item.labels.length) await form.getByLabel(/Labels/).fill(item.labels.join(", "));
      await form.getByRole("button", { name: "Review and add" }).click();
      const review = page.getByTestId("decision-preview");
      await expect(review.getByTestId("protected-effects")).toContainText("action proposed->ready");
      if (item.due_at) await expect(review).toContainText(zoneText);
      else await expect(review).toContainText("No deadline.");
      check(a, "the review shows the deadline in the project's zone, or no deadline", true);
      if (n === 0) await shot("W1-review");
      await review.getByRole("button", { name: "Add work item" }).click();
      await expect(page.getByTestId("work-added")).toContainText(item.title);
      const stored = ((await ownerGet("/records?kind=action&limit=200")).items as any[]).find(
        (r) => r.body.title === item.title,
      );
      ids[item.label] = stored?.id ?? "";
      check(a, "the item is stored ready", stored?.body.status === "ready", stored?.body.status);
      check(a, "its deadline is stored in UTC", stored?.body.due_at === item.due_at, stored?.body.due_at);
      check(a, "its description and labels are stored as typed",
        stored?.body.description === item.description && JSON.stringify(stored?.body.labels) === JSON.stringify(item.labels));
      record({
        id: `W1.${n + 1}`,
        via: "browser",
        request: { page: "/work", title: item.title, date: item.date, time: item.time, labels: item.labels },
        response: stored,
        outcome: stored ? "ok" : "error",
        assertions: a,
        created: { [item.label]: ids[item.label] ?? "" },
      });
    }
    await page.goto(`${origin}/work`);
    await expect(page.getByTestId("open-work").getByTestId("work-item")).toHaveCount(fx.items.length);
    await shot("W1-work-list");

    // ----- W2: the checks run from Now's inbox; the deadline events appear -------------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/now`);
      const inbox = page.getByTestId("event-inbox");
      await inbox.getByTestId("inbox-check").click();
      for (const item of fx.items.filter((i) => i.threshold !== null)) {
        const row = inbox.locator(`[data-testid="inbox-event"][data-subject="${ids[item.label]}"]`);
        await expect(row).toHaveAttribute("data-type", "deadline.approaching");
        await expect(row.getByTestId("inbox-reason")).toContainText(`deadline is within ${item.threshold} days`);
        await expect(row.getByTestId("inbox-subject")).toContainText(item.title);
        await expect(row.getByTestId("inbox-when")).toContainText(zoneText);
      }
      const tidy = fx.items.find((i) => i.threshold === null)!;
      await expect(inbox.locator(`[data-testid="inbox-event"][data-subject="${ids[tidy.label]}"]`)).toHaveCount(0);
      check(a, "the inbox shows each deadline event with its reason and time in the owner's zone", true);
      const listed = await ownerGet("/events?type=deadline.approaching&state=open&limit=200");
      const notices = Object.fromEntries(
        (listed.items as any[]).map((e) => [Object.keys(ids).find((k) => ids[k] === e.subject_ref.id), e.data.days_before]),
      );
      const expected = Object.fromEntries(fx.items.filter((i) => i.threshold !== null).map((i) => [i.label, i.threshold]));
      check(a, "each deadline fired its tightest threshold once (14, 7, 2 days)",
        (listed.items as any[]).length === Object.keys(expected).length && JSON.stringify(notices, Object.keys(expected).sort()) === JSON.stringify(expected, Object.keys(expected).sort()),
        notices);
      check(a, "the item without a deadline has no deadline event", !(tidy.label in notices));
      await shot("W2-inbox-deadlines");
      record({ id: "W2.1", via: "browser", request: { page: "/now", button: "Run due checks now" }, response: listed, outcome: "ok", assertions: a });
    }
  } finally {
    save();
  }

  // ----- The agent's session (MCP, run by the harness while this hub keeps serving) ---------
  writeFileSync(join(signals, "owner-1.done"), "done", "utf-8");
  await waitForAgent();

  const accepted = fx.items.find((i) => i.label === fx.accepted)!;
  const acceptedId = ids[accepted.label] ?? "";
  try {
    // ----- W5.1: the owner sees the item done ------------------------------------------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/work`);
      await expect(page.getByTestId("open-work").getByTestId("work-item")).toHaveCount(fx.items.length - 1);
      await page.getByText(/Done or cancelled \(1\)/).click();
      const card = page.getByTestId("closed-work").getByTestId("work-item").filter({ hasText: accepted.title });
      await expect(card).toHaveAttribute("data-status", "done");
      await shot("W5-work-done");
      await card.getByRole("link", { name: accepted.title }).click();
      const detail = page.getByTestId("work-item-detail");
      await expect(detail).toHaveAttribute("data-status", "done");
      await expect(detail).toContainText("Completed with 1 cited source(s).");
      check(a, "the Work page and the item's page show it done with its cited output", true);
      await shot("W5-item-done");
      const stored = await ownerGet(`/records/${acceptedId}`);
      check(a, "the owner sees the item done", stored.body.status === "done", stored.body.status);
      check(a, "with one cited output", stored.body.completion_evidence.length === 1);
      record({ id: "W5.1", via: "browser", request: { record: accepted.label }, response: stored, outcome: "ok", assertions: a });
    }
    // ----- W5.2: the owner acknowledges the item's deadline event with a note ---------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/now`);
      const inbox = page.getByTestId("event-inbox");
      const row = inbox.locator(`[data-testid="inbox-event"][data-type="deadline.approaching"][data-subject="${acceptedId}"]`);
      await row.getByTestId("inbox-note").fill(fx.ack_note);
      const answered = page.waitForResponse((r) => r.url().endsWith("/api/v1/events/acknowledge"));
      await row.getByTestId("inbox-ack").click();
      const reply = await answered;
      const acked = await reply.json();
      await expect(row).toHaveCount(0);
      await shot("W5-inbox-acknowledged");
      const stored = await ownerGet(`/events?state=acknowledged&type=deadline.approaching&subject_id=${acceptedId}`);
      check(a, "the event is acknowledged with the owner's note",
        stored.items.length === 1 && stored.items[0].acknowledged_note === fx.ack_note, stored.items);
      record({
        id: "W5.2",
        via: "browser",
        request: { event_ids: ["p1_deadline_event"], note: fx.ack_note },
        response: acked,
        outcome: reply.status() < 400 ? "ok" : "error",
        assertions: a,
        created: { p1_deadline_event: stored.items[0]?.event_id ?? "" },
      });
    }
  } finally {
    save();
    await owner.dispose().catch(() => undefined);
    await context.close().catch(() => undefined);
  }
  const failed = steps.flatMap((s) => s.assertions.filter((x) => !x.passed).map((x) => `${s.id}: ${x.claim}`));
  expect(failed).toEqual([]);
});
