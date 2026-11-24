// Automated accessibility checks (axe-core) on the core routes. This does not claim full
// WCAG conformance; manual keyboard/screen-reader review is separate.
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, shot, test } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  return bad.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

test("pairing screen has no serious or critical axe violations", async ({ page, hub }) => {
  await page.goto(hub.url);
  await expect(page.getByRole("heading", { name: "Pair this browser" })).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
});

for (const [name, path, ready] of [
  ["Now", "now", "objective"],
  ["Work items", "work", "add-work-item"],
  ["Notebook", "notebook", "entry-card"],
  ["Knowledge", "knowledge", "claim-card"],
  ["Papers", "papers", "claim-evidence"],
] as const) {
  test(`${name} has no serious or critical axe violations`, async ({ paired: page, hub }) => {
    await page.goto(`${hub.url}${path}`);
    await expect(page.getByTestId(ready).first()).toBeVisible();
    // (No "networkidle": the change feed keeps a long-poll open by design.)
    await expect(page.locator(".state-loading")).toHaveCount(0);
    expect(await seriousViolations(page)).toEqual([]);
    await shot(page, `a11y-${path}`);
  });
}

test("a work item page, with its edit form open, has no serious or critical axe violations", async ({
  paired: page,
  hub,
  api,
}) => {
  // Its own item, so the check does not depend on what earlier specs created.
  const id = `action_${crypto.randomUUID()}`;
  await api.post("/transactions", {
    schema_version: 1,
    project_id: hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations: [
      {
        op: "create",
        kind: "action",
        id,
        base_rev: 0,
        sensitivity: "normal",
        occurred_at: null,
        body: {
          title: `Accessibility check item ${Date.now()}`,
          reason: "Axe check of the work item page",
          source_refs: [],
          owner_ref: null,
          due_at: "2026-12-01T09:00:00Z",
          dependencies: [],
          status: "proposed",
          blocker: null,
          completion_evidence: [],
          commitment_item_id: null,
          description: "A description shown as text.",
          labels: ["a11y"],
        },
      },
    ],
  });
  await page.goto(`${hub.url}actions/${id}`);
  const detail = page.getByTestId("work-item-detail");
  await expect(detail).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
  await detail.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByTestId("edit-work-item")).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
  await shot(page, "a11y-work-item-edit");
});

test("meeting review and an open context panel have no serious or critical axe violations", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}meetings/${hub.meeting_id}`);
  await expect(page.getByTestId("meeting-item").first()).toBeVisible();
  await page.getByTestId("meeting-item").first().getByRole("button", { name: /Show evidence/ }).click();
  expect(await seriousViolations(page)).toEqual([]);
  await page.goto(`${hub.url}notebook`);
  await page
    .locator(`[id="${hub.seed.audit_entry_id}"]`)
    .getByRole("button", { name: /Open source plant-result.md/ })
    .click();
  await expect(page.getByTestId("context-panel").locator("li.cited").first()).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
});
