// 390px: readable and capture-capable with stacked panels; 1024x768: panel beside the list.
import type { Page } from "@playwright/test";
import { expect, nav, shot, test } from "./fixtures";

async function noHorizontalScroll(page: Page) {
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(client + 1);
}

test.describe("390px wide", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("destinations are readable without horizontal scrolling and capture works", async ({
    paired: page,
    hub,
    api,
  }) => {
    await page.goto(`${hub.url}now`);
    await expect(page.getByTestId("objective")).toBeVisible();
    await noHorizontalScroll(page);
    await shot(page, "mobile-390-now");
    for (const name of ["Work items", "Notebook", "Knowledge", "Papers"] as const) {
      await nav(page, name).click();
      await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
      // (No "networkidle": the change feed keeps a long-poll open by design.)
      await expect(page.locator(".state-loading")).toHaveCount(0);
      await noHorizontalScroll(page);
      await shot(page, `mobile-390-${name.toLowerCase().replace(" ", "-")}`);
    }

    await nav(page, "Now").click();
    await page.getByTestId("capture-cta").click();
    const title = `Phone note ${Date.now()}`;
    await page.getByLabel("Title", { exact: true }).fill(title);
    await page.getByLabel("Note", { exact: true }).fill("Captured at 390px.");
    await page.getByRole("button", { name: "Save note" }).click();
    await expect(page.getByTestId("capture-saved")).toBeVisible();
    await noHorizontalScroll(page);
    expect(
      (await api.records("entry")).filter((e) => e.body.title === title),
    ).toHaveLength(1);
  });

  test("the context panel stacks below the list at full width", async ({
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
    // Measure both boxes in one snapshot, in document coordinates: two separate
    // viewport-relative reads raced the focus scroll under load (decision 0009 item 11).
    const layout = () =>
      page.evaluate(() => {
        const main = document.querySelector("main")?.getBoundingClientRect();
        const box = document
          .querySelector('[data-testid="context-panel"]')
          ?.getBoundingClientRect();
        if (!main || !box) return null;
        return { gap: box.top - main.bottom, width: box.width };
      });
    await expect
      .poll(async () => (await layout())?.gap ?? -Infinity)
      .toBeGreaterThanOrEqual(-1);
    expect((await layout())?.width ?? 0).toBeGreaterThan(330);
    await noHorizontalScroll(page);
    await shot(page, "mobile-390-context-panel");
  });
});

test.describe("1024x768", () => {
  test.use({ viewport: { width: 1024, height: 768 } });

  test("desktop minimum: the context panel opens beside the list", async ({
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
    // One snapshot of both boxes, polled until layout settles (decision 0009 item 11).
    await expect
      .poll(() =>
        page.evaluate(() => {
          const main = document.querySelector("main")?.getBoundingClientRect();
          const box = document
            .querySelector('[data-testid="context-panel"]')
            ?.getBoundingClientRect();
          return main && box ? box.left - main.right : Number.NEGATIVE_INFINITY;
        }),
      )
      .toBeGreaterThanOrEqual(-1);
    await noHorizontalScroll(page);
    await shot(page, "desktop-1024-notebook-panel");
  });
});

test.describe("800x600 (a narrow desktop window)", () => {
  test.use({ viewport: { width: 800, height: 600 } });

  // Decision 0054: the "Connect an agent" guide stays inside the window and names the plugin.
  test("the agent guide opens inside the window and names the plugin setup", async ({ paired: page, hub }) => {
    await page.goto(`${hub.url}now`);
    await page.getByText("Connect an agent", { exact: true }).click();
    const body = page.getByTestId("agent-guide-body");
    await expect(body).toBeVisible();
    await expect(body).toContainText("--host claude-code-plugin --project project_");
    await expect(body).toContainText("--host claude-code --project project_");
    await expect(body).toContainText("/plugin install prometheos@prometheos-local");
    const box = await body.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(800);
    await noHorizontalScroll(page);
  });
});
