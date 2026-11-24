// Path 4: notebook entry -> exact retained source text with the cited span highlighted.
import { expect, shot, test } from "./fixtures";

test("path 4: audit a notebook entry down to the highlighted retained source span", async ({
  paired: page,
  hub,
  api,
}) => {
  const [start, end] = hub.seed.audit_lines;
  const version = await api.get(`/versions/${hub.seed.audit_version_id}/text`);
  const sourceLines: string[] = version.text.replace(/\n$/, "").split("\n");

  await page.goto(`${hub.url}notebook`);
  const entry = page.locator(`[id="${hub.seed.audit_entry_id}"]`);
  await expect(entry).toContainText("Plant result recorded");
  await expect(entry).toContainText("Finding");
  // Entries without a source say so (source indicators).
  await expect(page.getByTestId("entry-card").filter({ hasText: "Baseline imported" }).getByTestId("no-source")).toBeVisible();

  const button = entry.getByRole("button", { name: `Open source plant-result.md, lines ${start}–${end}` });
  await button.click();
  const panel = page.getByTestId("context-panel");
  await expect(panel.getByRole("heading", { name: "Source: plant-result.md" })).toBeFocused();
  const cited = panel.locator("li.cited");
  await expect(cited).toHaveCount(end - start + 1);
  await expect(cited.nth(0)).toHaveAttribute("data-line", String(start));
  await expect(cited.nth(0)).toContainText("1.80 kJ");
  await expect(cited.nth(1)).toContainText("1.90 kJ");
  // Exactly the retained text, line for line (no fabricated or rewritten excerpt).
  const shown = await panel.locator("ol.source-text > li").evaluateAll((lis) =>
    lis.map((li) => (li.textContent ?? "").replace(/^Cited: /, "").replace(/^ $/, "")),
  );
  expect(shown).toEqual(sourceLines);
  // The notebook list stays in place beside the panel.
  await expect(entry).toBeVisible();
  await shot(page, "audit-source-panel");
  await panel.getByRole("button", { name: /Close/ }).click();
  await expect(panel).toHaveCount(0);
  await expect(button).toBeFocused();
});

test("an unavailable source shows 'unavailable' with the reason and no excerpt (mocked response)", async ({
  paired: page,
  hub,
}) => {
  // The fixture retains every source, so the unavailable reply is simulated at the network
  // layer with the exact shape the hub uses (text:null plus reason).
  await page.route(`**/api/v1/versions/${hub.seed.audit_version_id}/text`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version_id: hub.seed.audit_version_id,
        source_id: "source_x",
        availability: "missing",
        text: null,
        reason: "the retained bytes are missing from the store",
        truncated: false,
        mime: "text/markdown",
        byte_length: 0,
      }),
    }),
  );
  await page.goto(`${hub.url}notebook`);
  await page
    .locator(`[id="${hub.seed.audit_entry_id}"]`)
    .getByRole("button", { name: /Open source plant-result.md/ })
    .click();
  const panel = page.getByTestId("context-panel");
  await expect(panel.getByTestId("source-unavailable")).toContainText("Source text unavailable");
  await expect(panel.getByTestId("source-unavailable")).toContainText(
    "the retained bytes are missing from the store",
  );
  await expect(panel.locator("ol.source-text")).toHaveCount(0);
});
