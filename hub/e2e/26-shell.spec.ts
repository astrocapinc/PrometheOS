// The 2026-10-18 UI/UX pass: the sidebar shell, the command palette (Ctrl K), keyboard
// shortcuts and the theme switch (dark by default). The palette only navigates or hands text to
// a page's own form; nothing here writes a record.
import AxeBuilder from "@axe-core/playwright";
import { request as pwRequest, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, nav, origin, shot, test, type HubInfo } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  return bad.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

const palette = (page: Page) => page.getByRole("dialog", { name: "Command palette" });
const paletteInput = (page: Page) => page.getByRole("combobox", { name: "Search pages, actions and records" });

test("the hub opens dark by default and stores no theme until the owner picks one", async ({ paired: page, hub }) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBeNull();
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");
  expect(await page.evaluate(() => window.localStorage.getItem("prometheos.hub.theme"))).toBeNull();
  await expect(page.getByTestId("ledger-status")).toContainText("Ledger connected");
});

test("the theme switch cycles dark, light and follow-Windows, and the choice survives a reload", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}knowledge`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  const toggle = page.getByTestId("theme-toggle");
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const dark = await background();

  await toggle.click();
  await expect(toggle).toHaveAttribute("data-theme-choice", "light");
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("light");
  expect(await background()).not.toBe(dark);
  expect(await page.evaluate(() => window.localStorage.getItem("prometheos.hub.theme"))).toBe("light");
  await page.reload();
  await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "light");
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("light");
  // The desktop window paints this colour before the page loads and sets its title bar from it.
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute("content", "#f6f7f9");
  await shot(page, "shell-light-knowledge");

  await page.getByTestId("theme-toggle").click();
  await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "system");
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("system");
  await page.getByTestId("theme-toggle").click();
  await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "dark");
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBeNull();
  expect(await background()).toBe(dark);
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute("content", "#0e1116");
});

test("the desktop window's ?theme= is taken once, stored and removed from the address", async ({ paired: page, hub }) => {
  await page.goto(`${hub.url}knowledge?theme=light`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("light");
  expect(new URL(page.url()).search).toBe("");
  expect(await page.evaluate(() => window.localStorage.getItem("prometheos.hub.theme"))).toBe("light");
  // Anything else is ignored (and still removed).
  await page.goto(`${hub.url}knowledge?theme=%3Cscript%3E`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  expect(new URL(page.url()).search).toBe("");
  expect(await page.evaluate(() => window.localStorage.getItem("prometheos.hub.theme"))).toBe("light");
});

test("Ctrl K opens the palette; typing filters pages and Enter opens one; Escape restores focus", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  const trigger = page.getByTestId("palette-trigger");
  await trigger.focus();
  await page.keyboard.press("Control+k");
  await expect(palette(page)).toBeVisible();
  await expect(paletteInput(page)).toBeFocused();
  await paletteInput(page).fill("approv");
  const first = palette(page).getByRole("option").filter({ hasText: "Approvals" }).first();
  await expect(first).toBeVisible();
  // Move the active option onto Approvals with the arrow keys, then open it.
  const options = palette(page).getByRole("option");
  const labels = await options.allTextContents();
  const target = labels.findIndex((l) => l.startsWith("Approvals"));
  expect(target).toBeGreaterThanOrEqual(0);
  for (let i = 0; i < target; i += 1) await page.keyboard.press("ArrowDown");
  await expect(options.nth(target)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
  await expect(palette(page)).toBeHidden();
  expect(new URL(page.url()).pathname).toBe("/approvals");

  // The button opens it too; Escape closes it and focus returns to the button.
  await page.getByTestId("palette-trigger").click();
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette(page)).toBeHidden();
  await expect(page.getByTestId("palette-trigger")).toBeFocused();
});

test("the palette finds records through the existing knowledge search and hands a note to Capture", async ({
  paired: page,
  hub,
  api,
}) => {
  const before = (await api.records("entry")).length;
  await page.goto(`${hub.url}knowledge`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  await page.keyboard.press("Control+k");
  await paletteInput(page).fill("energy");
  const records = palette(page).getByRole("group", { name: "Records" });
  await expect(records.getByRole("option").first()).toBeVisible();
  await expect(palette(page).getByRole("status")).toContainText("matching record");
  await shot(page, "shell-palette-records");

  // Every record kind the project search indexes is reachable: a meeting opens its page.
  await paletteInput(page).fill("Supervision meeting");
  // The meeting itself (its kind label is exactly "Meeting"), not one of its items.
  const meeting = records
    .getByRole("option")
    .filter({ hasText: "Supervision meeting" })
    .filter({ has: page.locator(".palette-hint", { hasText: /^Meeting$/ }) })
    .first();
  await expect(meeting).toBeVisible();
  await meeting.click();
  await expect(page.getByRole("heading", { level: 1, name: "Supervision meeting" })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe(`/meetings/${hub.meeting_id}`);
  await page.keyboard.press("Control+k");

  // "Capture a note" carries the typed text into the capture form; nothing is saved by the palette.
  await paletteInput(page).fill("Check regen limits at low SOC");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Capture" })).toBeVisible();
  await expect(page.getByLabel("Note", { exact: true })).toHaveValue("Check regen limits at low SOC");
  expect((await api.records("entry")).length).toBe(before);
});

const blur = (page: Page) => page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());

test("g then a letter goes to a page, c opens Capture, ? shows the shortcut sheet; text fields keep their keys", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  await blur(page);
  await page.keyboard.press("g");
  await page.keyboard.press("k");
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  await expect(nav(page, "Knowledge")).toHaveAttribute("aria-current", "page");

  // Typing "g" and "p" in a search field must not navigate.
  const search = page.getByLabel("Search findings, evidence, questions and claims");
  await search.fill("");
  await search.press("g");
  await search.press("p");
  await expect(search).toHaveValue("gp");
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();

  await blur(page);
  await page.keyboard.press("?");
  const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("Go to Papers");
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();

  // A bare "c" does nothing; Capture is "g" then "c".
  await blur(page);
  await page.keyboard.press("c");
  await page.waitForTimeout(300);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("c");
  await expect(page.getByRole("heading", { level: 1, name: "Capture" })).toBeVisible();
});

// Regression int11: the LaTeX editor lives in a shadow root, so its key events reached the
// window shortcut handler retargeted to a plain div, and typing "% cited" opened Capture.
// Typing in the workspace editor and in a textarea never navigates or opens anything.
test("typing in the LaTeX workspace editor or a textarea never triggers a shortcut", async ({ paired: page, hub }) => {
  // A plain textarea (the capture note).
  await page.goto(`${hub.url}capture`);
  await expect(page.getByRole("heading", { level: 1, name: "Capture" })).toBeVisible();
  const note = page.getByLabel("Note", { exact: true });
  await note.click();
  await page.keyboard.type("gk gp c ? gw done");
  await page.waitForTimeout(300);
  expect(new URL(page.url()).pathname).toBe("/capture");
  await expect(note).toHaveValue("gk gp c ? gw done");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeHidden();

  const path = `/papers/${hub.seed.latex_paper_id}/workspace`;
  await page.goto(`${hub.url}papers/${hub.seed.latex_paper_id}/workspace`);
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic LaTeX workspace paper" })).toBeVisible();
  await expect(page.getByTestId("save-state")).toHaveText("main.tex saved");
  await page.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  const typed = "% cited gk gp gc c ? g w";
  await page.keyboard.type(`\n${typed}`);
  await page.waitForTimeout(500);
  expect(new URL(page.url()).pathname).toBe(path);
  await expect(page.locator(".cm-content")).toContainText(typed);
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeHidden();
  // Leaving the draft unsaved keeps main.tex on disk unchanged for the other specs.
  await expect(page.getByTestId("save-state")).toHaveText("Unsaved changes in main.tex");

  // Even outside the editor, the workspace page takes no letter shortcuts.
  await page.getByRole("heading", { level: 1, name: "Synthetic LaTeX workspace paper" }).focus();
  await page.keyboard.press("g");
  await page.keyboard.press("k");
  await page.keyboard.press("?");
  await page.waitForTimeout(300);
  expect(new URL(page.url()).pathname).toBe(path);
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeHidden();
  // Ctrl K still opens the palette there.
  await page.keyboard.press("Control+k");
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");
});

test("a palette jump focuses the new page's heading; Escape closes the palette before the source panel", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}notebook`);
  await page
    .locator(`[id="${hub.seed.audit_entry_id}"]`)
    .getByRole("button", { name: /Open source plant-result.md/ })
    .click();
  const panel = page.getByTestId("context-panel");
  await expect(panel.locator("li.cited").first()).toBeVisible();
  await page.keyboard.press("Control+k");
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette(page)).toBeHidden();
  await expect(panel).toBeVisible();

  await page.keyboard.press("Control+k");
  await paletteInput(page).fill("papers");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Papers" })).toBeFocused();
});

test("the palette's search hands its query to the page already open", async ({ paired: page, hub }) => {
  await page.goto(`${hub.url}knowledge`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  await page.keyboard.press("Control+k");
  await paletteInput(page).fill("energy");
  await palette(page).getByRole("option", { name: /Search knowledge for/ }).click();
  await expect(page.getByLabel("Search findings, evidence, questions and claims")).toHaveValue("energy");
  await expect(page.getByTestId("search-results")).toContainText("energy");
  // Nothing is left pending for the next visit to another page.
  await nav(page, "Work items").click();
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue("");
});

test("at 800x600 the sidebar folds to icons with names kept, and nothing scrolls sideways", async ({
  paired: page,
  hub,
}) => {
  await page.setViewportSize({ width: 800, height: 600 });
  await page.goto(`${hub.url}knowledge`);
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  const sidebar = await page.locator(".sidebar").boundingBox();
  expect(sidebar?.width ?? 999).toBeLessThan(80);
  await expect(nav(page, "Papers")).toBeVisible();
  await nav(page, "Papers").click();
  await expect(page.getByRole("heading", { level: 1, name: "Papers" })).toBeVisible();
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(client + 1);
  await shot(page, "shell-800-papers");
});

test("under reduced motion nothing animates; the agent guide offers copy buttons for its commands", async ({
  paired: page,
  hub,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  await nav(page, "Knowledge").click();
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  const duration = await page.evaluate(() => getComputedStyle(document.querySelector("main") as Element).animationDuration);
  expect(parseFloat(duration)).toBeLessThan(0.01);

  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(hub.url).origin });
  await page.getByText("Connect an agent", { exact: true }).click();
  const guide = page.getByTestId("agent-guide-body");
  await guide.getByRole("button", { name: "Copy the plugin setup command" }).click();
  const shown = (await guide.locator("pre.command").first().textContent())?.trim();
  expect(shown).toContain("--host claude-code-plugin --project project_");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(shown);
  await expect(guide.getByRole("button", { name: "Copy the project folder setup command" })).toBeVisible();
});

test("an empty list says what will appear and how to fill it", async ({ paired: page, hub }) => {
  await page.route("**/api/v1/approvals", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ approvals: [] }) }),
  );
  await page.goto(`${hub.url}approvals`);
  await expect(page.getByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
  await expect(page.locator(".state-empty").first()).toContainText("the exact plan waits here for you");
  await page.unroute("**/api/v1/approvals");
});

for (const theme of ["dark", "light"] as const) {
  test(`the shell and the open palette have no serious or critical axe violations (${theme})`, async ({
    paired: page,
    hub,
  }) => {
    await page.goto(`${hub.url}now`);
    await expect(page.getByTestId("objective")).toBeVisible();
    if (theme === "light") {
      await page.getByTestId("theme-toggle").click();
      await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "light");
    }
    await page.goto(`${hub.url}knowledge`);
    await expect(page.getByTestId("claim-card").first()).toBeVisible();
    await expect(page.locator(".state-loading")).toHaveCount(0);
    expect(await seriousViolations(page)).toEqual([]);
    await page.keyboard.press("Control+k");
    await paletteInput(page).fill("now");
    await expect(palette(page).getByRole("option").first()).toBeVisible();
    expect(await seriousViolations(page)).toEqual([]);
    await page.keyboard.press("Escape");
    if (theme === "light") await page.evaluate(() => window.localStorage.removeItem("prometheos.hub.theme"));
  });
}

test("pushed run progress: a run an agent starts appears on Runs within seconds, before the slow poll", async ({
  paired: page,
  hub,
}) => {
  const agentHub = hub as HubInfo & { agent_token: string };
  await page.goto(`${hub.url}runs`);
  await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
  await page.waitForTimeout(1000); // the watch is waiting now
  const agent = await pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${agentHub.agent_token}` },
  });
  try {
    const name = `watch-e2e-${crypto.randomUUID().slice(0, 8)}.md`;
    writeFileSync(join(hub.seed.latex_folder, name), "A result for the run watch.\n", "utf-8");
    const started = await agent.post("/api/v1/sources/plans", {
      data: { root_alias: "research", paths: [`latex/${name}`], retention: "copy" },
    });
    expect(started.status(), await started.text()).toBe(200);
    const runId = String((await started.json()).run_id);
    // The page polls only every 15 s now; within 5 s only the pushed watch can show it.
    await expect(page.locator(`[data-testid="run-card"][data-run-id="${runId}"]`)).toBeVisible({ timeout: 5000 });
  } finally {
    await agent.dispose();
  }
});
