// C21-WALK, the hub leg (CP21.4; decision 0091). The owner's steps in the hub that
// `prometheos start` launched, at the home's own host name, against store A.
// tools/walk_cp21.py runs the launcher and the agent's steps over the real MCP server between
// them and checks the store; the two hand over through signal files.
//
//   W1.1 a second `prometheos start` opens a paired browser on the running hub: the page takes
//        the launch code from the fragment, drops it from the history entry and pairs
//   W2.1 capture a note in the hub
//   (agent: W3.1 suggests a work item, W3.2 asks to acknowledge its event -> approval_required,
//    W3.3 a boundary control: resuming before the owner decides is refused)
//   W4.1 the request waits on the Approvals page
//   W4.2 the owner reviews its plan and grants it
//   (agent: W5.1 resumes the granted request)
//   W6.1 the core restarts (`prometheos stop`, `prometheos start --no-open`): a reload resumes
//        the session from the remembered browser, without pairing
//   L4   after the hub is killed and started again, a reload resumes again
//   L5   the owner revokes this browser on the Recovery page and is signed out
import { expect, type Page, type Request } from "@playwright/test";
import { guardedTest as test } from "../e2e/boundary-guard";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

type Info = { url: string; host: string; signals: string; shots: string; steps: string };
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

const NOW = { level: 1, name: "Now" } as const;
const PAIR = { level: 1, name: "Pair this browser" } as const;

test("C21-WALK hub leg", async ({ page, context }) => {
  // Every request line the browser sends, to show that no launch code ever reaches one.
  const requests: string[] = [];
  page.on("request", (request: Request) => requests.push(`${request.method()} ${request.url()}`));
  const since = (mark: number) => requests.slice(mark).map((line) => new URL(line.split(" ")[1] ?? "").pathname);

  signal("ready");
  const { url } = await waitFor("pair-url", 120);
  const code = new URL(url).hash.replace(/^#pair=/, "");

  // W1.1: the launch code in the fragment pairs this browser; it is gone from the history entry.
  await page.goto(url);
  await expect(page.getByRole("heading", NOW)).toBeVisible({ timeout: 30_000 });
  const session = await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()));
  const cookies = await context.cookies();
  const device = cookies.find((c) => c.name.startsWith("prometheos_device_"));
  await shot(page, "W1.1-paired");
  record({
    id: "W1.1", url_host: new URL(page.url()).host, paired: session.paired, hash_after: await page.evaluate(() => window.location.hash),
    href_has_code: page.url().includes(code), request_lines_with_code: requests.filter((r) => r.includes(code)).length,
    device_cookie: device ? { name: device.name, host_only: !device.domain.startsWith("."), domain: device.domain, http_only: device.httpOnly, same_site: device.sameSite } : null,
  });
  signal("owner-opened");
  await waitFor("opened-checked");

  // W2.1: a note captured in the hub.
  await page.goto(new URL("/capture", info.url).toString());
  await page.getByLabel("Title", { exact: true }).fill("Walk note from the hub");
  await page.getByLabel("Note", { exact: true }).fill("Captured in the browser hub (C21-WALK).");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("capture-saved")).toContainText("Walk note from the hub");
  await shot(page, "W2.1-captured");
  record({ id: "W2.1", saved: true });
  signal("owner-captured");

  const asked = await waitFor("agent-requested");

  // W4.1: the request waits on the Approvals page.
  await page.goto(new URL("/approvals", info.url).toString());
  const card = page.locator(`[data-testid="approval-pending"][data-approval-id="${asked.approval_id}"]`);
  await expect(card).toBeVisible();
  record({ id: "W4.1", shown_on_approvals_page: true });

  // W4.2: reviewed and granted.
  await expect(card.getByTestId("approval-grant")).toBeDisabled();
  await card.getByTestId("approval-review").click();
  await expect(card.getByTestId("approval-plan")).toBeVisible();
  const planDigestShown = ((await card.textContent()) ?? "").includes(asked.plan_sha256);
  await card.getByTestId("approval-grant").click();
  await expect(card).toHaveCount(0);
  await expect(page.locator(`[data-testid="approval-granted"][data-approval-id="${asked.approval_id}"]`)).toBeVisible();
  await shot(page, "W4.2-granted");
  record({ id: "W4.2", granted_in_hub: true, plan_sha256: asked.plan_sha256, plan_digest_shown: planDigestShown });
  signal("owner-granted");

  await waitFor("agent-resumed");

  // W6.1: the core restarts; the session resumes from the remembered browser. Requests are
  // counted from before the restart: the open page may resume on its own as soon as its next
  // request fails (A7), before the reload.
  let mark = requests.length;
  signal("owner-restart");
  await waitFor("restarted", 180);
  await page.goto(new URL("/now", info.url).toString());
  await expect(page.getByRole("heading", NOW)).toBeVisible({ timeout: 30_000 });
  const resumed = since(mark);
  await shot(page, "W6.1-resumed");
  record({
    id: "W6.1", resumed_with_device: resumed.includes("/api/v1/device/resume"), paired_again: resumed.includes("/api/v1/pair"),
    pairing_screen_shown: await page.getByRole("heading", PAIR).isVisible(),
  });
  const current = (await context.cookies()).find((c) => c.name.startsWith("prometheos_device_"));
  mark = requests.length;
  signal("owner-resumed", { cookie_name: current?.name, cookie_value: current?.value });

  // L4: the hub is killed and started again; the session resumes again.
  await waitFor("killed-restarted", 240);
  await page.goto(new URL("/now", info.url).toString());
  await expect(page.getByRole("heading", NOW)).toBeVisible({ timeout: 30_000 });
  const again = since(mark);
  record({ id: "L4", resumed_with_device: again.includes("/api/v1/device/resume"), paired_again: again.includes("/api/v1/pair") });

  // L5: the owner revokes this browser on the Recovery page and is signed out.
  await page.goto(new URL("/recovery", info.url).toString());
  const devices = page.getByTestId("recovery-devices");
  await expect(devices).toBeVisible();
  const listed = await devices.getByTestId("device-revoke").count();
  await shot(page, "L5-remembered-browsers");
  await devices.getByTestId("device-revoke").first().click();
  await expect(page.getByRole("heading", PAIR)).toBeVisible({ timeout: 15_000 });
  await shot(page, "L5-signed-out");
  mark = requests.length;
  await page.goto(new URL("/now", info.url).toString());
  await expect(page.getByRole("heading", PAIR)).toBeVisible({ timeout: 15_000 });
  record({
    id: "L5", devices_listed: listed, signed_out: true, still_signed_out_after_reload: true,
    resume_tried_after_reload: since(mark).includes("/api/v1/device/resume"),
    request_lines_with_code: requests.filter((r) => r.includes(code)).length,
  });
  signal("owner-revoked");
  await waitFor("done", 120);
});
