// The built app runs under the hub's strict CSP with no violations, keeps credentials out
// of web storage and URLs, and renders record text as text (never HTML).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, nav, test } from "./fixtures";

const DIST = join(fileURLToPath(new URL(".", import.meta.url)), "..", "dist");
// The suite serves the assets shipped in the Python package unless HUB_STATIC=dist.
const SERVED =
  process.env.HUB_STATIC === "dist"
    ? DIST
    : join(DIST, "..", "..", "src", "prometheos", "hub", "static");

test("built index.html has no inline script or style", () => {
  const html = readFileSync(join(SERVED, "index.html"), "utf-8");
  expect(html).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/i);
  expect(html).not.toMatch(/<style[\s>]/i);
  expect(html).not.toMatch(/\sstyle=/i);
  expect(html).not.toMatch(/https?:\/\//i);
});

test("every destination runs under the hub CSP without violations", async ({ paired: page, hub }) => {
  const problems: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) problems.push(m.text());
  });
  page.on("pageerror", (e) => problems.push(String(e)));
  const response = await page.goto(`${hub.url}now`);
  expect(response?.headers()["content-security-policy"]).toContain("script-src 'self'");
  await expect(page.getByTestId("objective")).toBeVisible();
  for (const name of ["Notebook", "Knowledge", "Papers", "Now"] as const) {
    await nav(page, name).click();
    await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  }
  await page.goto(`${hub.url}meetings/${hub.meeting_id}`);
  await expect(page.getByTestId("meeting-item").first()).toBeVisible();
  expect(problems).toEqual([]);
});

test("record text containing markup is shown as plain text, not executed", async ({
  paired: page,
  hub,
  api,
}) => {
  const title = `Markup note ${Date.now()}`;
  const payload = '<img src=x onerror="window.__pwned=1"><b>bold?</b> **not markdown**';
  await api.createNote(title, payload);
  await page.goto(`${hub.url}notebook`);
  const card = page.getByTestId("entry-card").filter({ hasText: title });
  await expect(card.locator(".entry-text")).toHaveText(payload);
  await expect(card.locator("img, b")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
});
