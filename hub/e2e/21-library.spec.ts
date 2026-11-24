// A02, CP11 S11.1 (decision 0060): the Library section of Knowledge. The owner plans the import
// of the synthetic PDFs in research/library (hostile files refused with reasons), commits it,
// searches page text and opens a located page. Every outcome is checked against stored state
// through the API; screenshots supplement the assertions.
import { expect, shot, test } from "./fixtures";

type Work = { work_id: string; title: string; page_count: number; pages_without_text: number; pdf_version_id: string };
type Hit = { work_id: string; title: string; page: number; start: number; end: number; quote_sha256: string; snippet: { match: string } };

test("the owner imports a PDF folder, searches it and reads a located page", async ({ paired: page, hub, api }) => {
  const before = (await api.get<{ total: number }>("/library/works")).total;
  expect(before).toBe(0);
  await page.goto(`${hub.url}knowledge`);
  await page.getByTestId("knowledge-library-link").getByRole("link", { name: "Open the library" }).click();
  await expect(page.getByRole("heading", { name: "Library", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Knowledge", exact: true }).first()).toHaveAttribute("aria-current", "page");

  // Plan: every PDF of the folder with its action, or refused with its reason.
  await expect(page.getByTestId("library-root")).toHaveValue("research");
  await page.getByTestId("library-path").fill("library");
  await page.getByTestId("library-plan").click();
  const plan = page.getByTestId("library-plan-result");
  await expect(plan).toBeVisible({ timeout: 60_000 });
  const rows = page.getByTestId("library-plan-items").locator("tbody tr");
  await expect(rows).toHaveCount(7);
  await expect(page.locator('tr[data-action="duplicate"]')).toHaveCount(1);
  await expect(page.locator('tr[data-action="create"]')).toHaveCount(6);
  const refused = page.getByTestId("library-refused");
  for (const reason of ["encrypted", "malformed", "truncated", "too_many_pages", "not_pdf"]) {
    await expect(refused.locator(`li[data-reason="${reason}"]`)).toHaveCount(1);
  }
  await expect(page.getByTestId("library-plan-counts")).toContainText("6 new");
  await shot(page, "library-plan");

  // Commit: the works the page lists are the stored ones.
  await page.getByTestId("library-commit").click();
  await expect(page.getByTestId("library-commit-result")).toContainText("Imported 6 file(s)", { timeout: 60_000 });
  const works = (await api.get<{ total: number; works: Work[] }>("/library/works?limit=200")).works;
  expect(works).toHaveLength(6);
  await expect(page.getByTestId("library-works").locator("li")).toHaveCount(6);
  const drag = works.find((w) => w.title.startsWith("Aerodynamic drag"));
  expect(drag?.page_count).toBe(4);
  expect(drag?.pages_without_text).toBe(1);
  await expect(page.locator(`[data-work-id="${drag?.work_id}"]`)).toContainText("DOI 10.5555/synthetic.2026.001");
  await shot(page, "library-works");

  // Search: located snippets, the same hits as the API.
  await page.getByTestId("library-query").fill("drag coefficient");
  await page.getByTestId("library-search").click();
  const stored = (await api.get<{ hits: Hit[] }>("/library/search?query=drag%20coefficient&limit=20")).hits;
  expect(stored.length).toBeGreaterThan(2);
  const cards = page.getByTestId("library-hits").locator("li.card");
  await expect(cards).toHaveCount(stored.length);
  await expect(cards.first().locator("mark")).toHaveText(stored[0].snippet.match);
  await shot(page, "library-search");

  // Open page 2 of the drag paper at the hit: the reader shows that page with the span marked.
  const hit = stored.find((h) => h.work_id === drag?.work_id && h.page === 2);
  expect(hit).toBeTruthy();
  const card = cards.filter({ hasText: "characters " + hit!.start + "–" + hit!.end }).filter({ hasText: hit!.title }).first();
  await card.getByRole("button", { name: "Open page 2" }).click();
  const text = page.getByTestId("library-page-text");
  await expect(text).toHaveAttribute("data-page", "2");
  await expect(text.locator("mark")).toHaveText("drag coefficient");
  const read = await api.get<{ text: string }>(`/library/works/${drag!.work_id}/read?page=2&max_chars=8000`);
  await expect(text).toHaveText(read.text);
  await shot(page, "library-reader");
  await page.getByTestId("library-next").click();
  await expect(text).toHaveAttribute("data-page", "3");
  await expect(page.getByTestId("library-page-note")).toContainText("no text layer");

  // Quotation checks an agent makes against the stored page text.
  const where = { work_id: drag!.work_id, page: 2, start: hit!.start, end: hit!.end };
  const match = await api.post<{ result: string }>("/library/quote-check", { ...where, quote: "drag coefficient" });
  expect(match.result).toBe("match");
  const altered = await api.post<{ result: string; true_text: string }>("/library/quote-check", { ...where, quote: "drag coefficients" });
  expect(altered.result).toBe("mismatch");
  expect(altered.true_text).toBe("drag coefficient");
});
