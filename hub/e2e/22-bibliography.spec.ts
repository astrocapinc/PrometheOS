// A02, CP11 S11.2 (decision 0062): bibliographies in the Library. The owner plans and commits the
// synthetic .bib/.ris/.csv files of research/bibliography (hostile files refused with reasons),
// sees the merged works and a duplicate candidate, keeps the pair separate (an owner decision in
// the hub), exports BibTeX and annotates a page span. Every outcome is checked against stored
// state through the API; screenshots supplement the assertions.
import { expect, shot, test } from "./fixtures";

type Work = { work_id: string; title: string; identifiers: Record<string, string>; sources: number; page_count: number | null };
type Candidate = { candidate_id: string; kind: string; works: { work_id: string; title: string }[] };
type Hit = { work_id: string; page: number; start: number; end: number; quote_sha256: string };

test("the owner imports bibliographies, resolves a candidate, exports and annotates", async ({ paired: page, hub, api }) => {
  // The PDF journey (21) may have run first; import the PDFs here when it has not.
  let works = (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  if (!works.some((w) => w.page_count !== null)) {
    const planned = await api.post<{ result: { plan_id: string } }>("/library/plans", { root_alias: "research", path: "library", wait_seconds: 20 });
    await api.post(`/library/plans/${planned.result.plan_id}/commit`, { wait_seconds: 20 });
  }
  await page.goto(`${hub.url}knowledge/library`);
  await expect(page.getByRole("heading", { name: "Library", level: 1 })).toBeVisible();

  // Plan: the three bibliography files with their entries; the hostile ones refused.
  await page.getByTestId("library-path").fill("bibliography");
  await page.getByTestId("library-plan").click();
  await expect(page.getByTestId("library-plan-result")).toBeVisible({ timeout: 60_000 });
  const bibRows = page.locator('tr[data-kind="bibliography"]');
  await expect(bibRows).toHaveCount(6); // refs.bib, refs.ris, refs.csv, malformed, deep, two-dois
  const refused = page.getByTestId("library-refused");
  for (const reason of ["binary", "too_many_entries", "not_utf8"]) {
    await expect(refused.locator(`li[data-reason="${reason}"]`)).toHaveCount(1);
  }
  await page.locator('tr[data-kind="bibliography"]', { hasText: "refs.bib" }).getByTestId("library-bib-entries").locator("summary").click();
  await expect(page.locator('li[data-entry-action="merge"]').first()).toBeVisible();
  await shot(page, "bibliography-plan");
  await page.getByTestId("library-commit").click();
  await expect(page.getByTestId("library-commit-result")).toBeVisible({ timeout: 60_000 });

  // Exact duplicates are one work, with every source.
  works = (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  const bearing = works.filter((w) => w.identifiers.doi === "10.5555/synthetic.2026.020");
  expect(bearing).toHaveLength(1);
  expect(bearing[0]?.sources).toBe(2);
  await expect(page.locator(`[data-work-id="${bearing[0]?.work_id}"]`)).toContainText("2 bibliography source(s)");

  // The similar pair is only proposed; the owner keeps it separate; it is not proposed again.
  const before = (await api.get<{ candidates: Candidate[] }>("/library/dedup")).candidates;
  const windage = before.find((c) => c.works.some((w) => w.title.startsWith("Windage")));
  expect(windage?.kind).toBe("duplicate");
  const cards = page.getByTestId("library-candidate");
  await expect(cards).toHaveCount(before.length);
  await shot(page, "bibliography-candidates");
  const card = cards.filter({ hasText: "Windage in sealed housings: a survey" });
  await card.getByTestId("library-distinct").click();
  await expect(card.getByTestId("library-confirm")).toContainText("not proposed again");
  await card.getByTestId("library-confirm-yes").click();
  await expect(cards).toHaveCount(before.length - 1);
  const after = await api.get<{ candidates: Candidate[]; decisions: { choice: string }[] }>("/library/dedup");
  expect(after.candidates.find((c) => c.candidate_id === windage?.candidate_id)).toBeUndefined();
  expect(after.decisions.map((d) => d.choice)).toContain("distinct");
  // The decision can be undone: the pair is proposed again; then kept separate once more.
  await page.getByTestId("library-decisions").getByTestId("library-undo").first().click();
  await expect(cards).toHaveCount(before.length);
  const again = cards.filter({ hasText: "Windage in sealed housings: a survey" });
  await again.getByTestId("library-distinct").click();
  await again.getByTestId("library-confirm-yes").click();
  await expect(cards).toHaveCount(before.length - 1);

  // An import's version link is listed and can be undone; an undone link is not a dead end: the
  // owner links the pair again from the decisions (review round 2, R2-F2).
  type Link = { preprint: string; published: string; origin: string };
  type Dedup = { links: Link[]; decisions: { choice: string; work_a: string; work_b: string }[] };
  const linked = await api.get<Dedup>("/library/dedup");
  const fromImports = linked.links.filter((l) => l.origin === "import");
  const link = fromImports[0];
  expect(link).toBeTruthy();
  const linkRows = page.getByTestId("library-link");
  await expect(linkRows).toHaveCount(fromImports.length);
  await linkRows.first().getByTestId("library-link-undo").click();
  const unlinked = page.getByTestId("library-decisions").locator('li[data-choice="unlinked"]');
  await expect(unlinked).toHaveCount(1);
  await expect(linkRows).toHaveCount(fromImports.length - 1);
  let state = await api.get<Dedup>("/library/dedup");
  expect(state.links.some((l) => l.preprint === link!.preprint && l.published === link!.published)).toBe(false);
  expect(state.decisions.map((d) => d.choice)).toContain("unlinked");
  await shot(page, "bibliography-link-undone");
  await unlinked.getByTestId("library-relink").click();
  await expect(page.getByTestId("library-decisions").locator('li[data-choice="versions"]')).toHaveCount(1);
  await expect(unlinked).toHaveCount(0);
  state = await api.get<Dedup>("/library/dedup");
  expect(state.links).toContainEqual(expect.objectContaining({ preprint: link!.preprint, published: link!.published, origin: "decision" }));
  expect(state.decisions.map((d) => d.choice)).not.toContain("unlinked");

  // Export: stable keys and the work IDs; the same content as the API's.
  await page.getByTestId("library-export-format").selectOption("bibtex");
  await page.getByTestId("library-export-run").click();
  const exported = await api.post<{ content: string; count: number }>("/library/export", { format: "bibtex" });
  await expect(page.getByTestId("library-export-content")).toHaveValue(exported.content);
  expect(exported.content).toContain("prometheos_work = {");
  await shot(page, "bibliography-export");

  // Annotate a located span in the reader; the annotation is stored and listed.
  const drag = works.find((w) => w.identifiers.doi === "10.5555/synthetic.2026.001");
  expect(drag?.page_count).toBe(4);
  await page.getByTestId("library-query").fill("drag coefficient");
  await page.getByTestId("library-search").click();
  const hits = (await api.get<{ hits: Hit[] }>("/library/search?query=drag%20coefficient&limit=20")).hits;
  const hit = hits.find((h) => h.work_id === drag?.work_id && h.page === 2);
  expect(hit).toBeTruthy();
  const hitCard = page.getByTestId("library-hits").locator("li.card").filter({ hasText: `characters ${hit!.start}–${hit!.end}` }).first();
  await hitCard.getByRole("button", { name: "Open page 2" }).click();
  await page.getByTestId("library-annotation-text").fill("Compare with our rig: 0.0045 at 10 Pa.");
  await page.getByTestId("library-annotate").click();
  await expect(page.getByTestId("library-annotations").locator("li.card")).toHaveCount(1);
  const listed = await api.get<{ annotations: { text: string; page: number; start: number; end: number }[] }>(
    `/library/works/${drag!.work_id}/annotations`,
  );
  expect(listed.annotations).toHaveLength(1);
  expect(listed.annotations[0]).toMatchObject({ text: "Compare with our rig: 0.0045 at 10 Pa.", page: 2, start: hit!.start, end: hit!.end });
  await shot(page, "bibliography-annotation");
});
