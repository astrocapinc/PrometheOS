// Keyboard-only navigation of the four destinations, skip link, visible focus and the
// context panel's focus handling.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

async function tabTo(
  page: Page,
  matcher: (el: { text: string; tag: string; label: string }) => boolean,
) {
  for (let i = 0; i < 60; i += 1) {
    await page.keyboard.press("Tab");
    const el = await page.evaluate(() => {
      const a = document.activeElement as HTMLElement | null;
      return {
        text: (a?.textContent ?? "").trim(),
        tag: a?.tagName ?? "",
        label: a?.getAttribute("aria-label") ?? "",
      };
    });
    if (matcher(el)) return;
  }
  throw new Error("element not reached by Tab");
}

test("keyboard only: skip link, then each primary destination by Tab and Enter, with visible focus", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();

  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to main content" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main#main")).toBeFocused();

  for (const name of ["Notebook", "Knowledge", "Papers", "Now"]) {
    await page.locator("body").focus();
    await page.evaluate(() =>
      (document.activeElement as HTMLElement | null)?.blur(),
    );
    await tabTo(page, (el) => el.tag === "A" && el.text === name);
    const outline = await page.evaluate(
      () => getComputedStyle(document.activeElement as Element).outlineStyle,
    );
    expect(outline).not.toBe("none");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { level: 1, name })).toBeFocused();
    expect(new URL(page.url()).pathname).toBe(`/${name.toLowerCase()}`);
    await expect(
      page
        .getByRole("navigation", { name: "Primary" })
        .getByRole("link", { name, exact: true }),
    ).toHaveAttribute("aria-current", "page");
  }
  // Browser back returns to the previous destination.
  await page.goBack();
  await expect(
    page.getByRole("heading", { level: 1, name: "Papers" }),
  ).toBeVisible();
});

test("keyboard only: open a source in the context panel and close it with Escape", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}notebook`);
  await expect(page.getByTestId("entry-card").first()).toBeVisible();
  await tabTo(
    page,
    (el) =>
      el.tag === "BUTTON" && el.label.startsWith("Open source plant-result.md"),
  );
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", { name: "Source: plant-result.md" }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("context-panel")).toHaveCount(0);
  // Focus returns to the opener one task after the panel unmounts (hub.tsx setTimeout 0);
  // reading activeElement immediately raced under load (decision 0009 item 8).
  await expect
    .poll(() =>
      page.evaluate(
        () => document.activeElement?.getAttribute("aria-label") ?? "",
      ),
    )
    .toContain("Open source plant-result.md");
});
