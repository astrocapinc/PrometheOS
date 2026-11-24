// C11-WALK, hub half (tools/walk_cp11.py; spec/10 CP11.4). The owner's steps on the real built hub
// over a fresh store that has no research folder: the owner adds the papers folder, imports its
// PDFs and bibliography, imports selected Zotero items (read-only, from a stub of the local API),
// searches, builds a source-linked matrix over three works (full text, full text and an
// abstract-only Zotero item), classifies by a rule and by a manual choice, cites a verified span
// (a misquote is refused verbatim) and reviews. Then the test hands over (WALK_SIGNALS/owner-1.done)
// to the agent's session, which the harness runs over the real MCP server while this hub keeps
// serving: the agent reads the same matrix, records an assumption, a finding relying on it and a
// correction. Afterwards the owner exports the brief, applies the correction (the row and the brief
// are flagged) and re-reviews the finding (the row's flag clears).
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
interface Fixture {
  papers: string;
  sentence: string;
  losses: string;
  review_sentence: string;
  columns: [string, string, string?][];
  rules: { method_text: string; method_value: string; pressure_text: string; pressure_unit: string; finding_text: string };
  titles: { drag: string; kers: string; bearing: string };
  zotero: { collection: string; deselect: string };
}
type Work = { work_id: string; title: string; access_state: string; page_count: number | null; sources: number; identifiers: Record<string, string> };
type Cell = { column_id: string; value_text: string; state: string; basis: string; evidence: { where: { page?: number; start?: number; end?: number } }[] };
type Row = { work_id: string; access_state: string; needs_review: boolean; cells: Cell[] };
type View = { matrix: { matrix_id: string; columns: { id: string }[] }; rows: Row[]; briefs: { version_id: string; needs_review: boolean }[] };

const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;
const fx = JSON.parse(process.env.WALK_FIXTURE ?? "{}") as Fixture;
const stepsPath = process.env.WALK_STEPS ?? "walk-steps.json";
const signals = process.env.WALK_SIGNALS ?? "walk-signals";
const shots = process.env.WALK_SHOTS ?? "walk-shots";
const origin = info.url.replace(/\/$/, "");
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
async function waitForAgent(): Promise<Record<string, string>> {
  const until = Date.now() + 420_000;
  while (Date.now() < until) {
    if (existsSync(join(signals, "agent.failed"))) {
      throw new Error(`the agent's session failed: ${readFileSync(join(signals, "agent.failed"), "utf-8")}`);
    }
    if (existsSync(join(signals, "agent.done"))) {
      return JSON.parse(readFileSync(join(signals, "agent.done"), "utf-8")) as Record<string, string>;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("the agent's session did not finish in time");
}

/** Select ``text`` inside the page reader's text (as a person would with the mouse). */
async function selectInReader(page: Page, text: string): Promise<void> {
  await expect(page.getByTestId("library-page-text")).toContainText(text);
  const found = await page.evaluate((wanted) => {
    const pre = document.querySelector('[data-testid="library-page-text"]');
    if (!pre) return false;
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    let all = "";
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      nodes.push(n as Text);
      all += (n as Text).data;
    }
    const at = all.indexOf(wanted);
    if (at < 0) return false;
    const locate = (offset: number): [Text, number] => {
      let seen = 0;
      for (const node of nodes) {
        if (offset <= seen + node.data.length) return [node, offset - seen];
        seen += node.data.length;
      }
      const last = nodes[nodes.length - 1]!;
      return [last, last.data.length];
    };
    const range = document.createRange();
    const [startNode, startOffset] = locate(at);
    const [endNode, endOffset] = locate(at + wanted.length);
    range.setStart(startNode, startOffset);
    range.setEnd(endNode, endOffset);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  }, text);
  expect(found, `"${text}" is on the page`).toBe(true);
}

test("C11-WALK: the owner's steps on the real hub", async ({ browser }) => {
  const owner = await pairedOwner();
  const get = async (path: string): Promise<any> => {
    const reply = await owner.get(`/api/v1${path}`);
    expect(reply.status(), `${path}: ${await reply.text()}`).toBe(200);
    return reply.json();
  };
  const context = await browser.newContext();
  const page = await context.newPage();
  await pairPage(page);
  mkdirSync(shots, { recursive: true });
  const shot = async (name: string) => page.screenshot({ path: join(shots, `${name}.png`), fullPage: true });
  const record = (s: Step) => {
    steps.push(s);
    save();
  };
  const works = async () => (await get("/library/works?limit=200")).works as Work[];
  const view = async (matrixId: string) => (await get(`/matrix/${matrixId}`)) as View;
  const cellOf = (row: Row | undefined, id: string) => row?.cells.find((c) => c.column_id === id);
  const names: Record<string, string> = {};
  let matrixId = "";
  let drag!: Work;
  let kers!: Work;
  let bearing!: Work;

  try {
    // ----- M1.1: the owner adds the papers folder (the project starts with none) -------------
    {
      const a: Assertion[] = [];
      const before = await get("/project");
      await page.goto(`${origin}/knowledge/library`);
      await expect(page.getByTestId("library-no-roots")).toContainText("Research folders below");
      const panel = page.getByTestId("research-folders");
      await panel.getByTestId("research-folder-alias").fill("papers");
      await panel.getByTestId("research-folder-path").fill(fx.papers);
      await panel.getByTestId("research-folder-add").click();
      await expect(page.getByTestId("library-plan")).toBeVisible();
      await shot("M1.1-folder-added");
      const roots = await get("/project/roots");
      const after = await get("/project");
      check(a, "the project lists the papers folder, available (the hub never returns its path)",
        JSON.stringify(roots.roots) === JSON.stringify([{ alias: "papers", available: true }]), roots.roots);
      check(a, "adding it is one audited settings revision", after.settings_rev === before.settings_rev + 1);
      record({ id: "M1.1", via: "browser", request: { alias: "papers" }, response: roots, outcome: "ok", assertions: a });
    }

    // ----- M1.2: import the PDFs and the bibliography ------------------------------------------
    {
      const a: Assertion[] = [];
      await page.getByTestId("library-root").selectOption("papers");
      await page.getByTestId("library-plan").click();
      await expect(page.getByTestId("library-plan-result")).toBeVisible({ timeout: 60_000 });
      await page.getByTestId("library-commit").click();
      await expect(page.getByTestId("library-commit-result")).toBeVisible({ timeout: 90_000 });
      await shot("M1.2-imported");
      const all = await works();
      drag = all.find((w) => w.title.startsWith("Aerodynamic"))!;
      kers = all.find((w) => w.title.startsWith("Kinetic"))!;
      check(a, "the drag paper is a full-text work of 4 pages", drag?.access_state === "full_text" && drag?.page_count === 4);
      check(a, "the review paper is a full-text work", kers?.access_state === "full_text");
      check(a, "the bibliography's entries became works (metadata only)", all.filter((w) => w.access_state === "metadata_only").length >= 4,
        all.map((w) => [w.title, w.access_state]));
      check(a, "the PDF work took the bibliography entry with its DOI (one work, two kinds of source)", drag.sources >= 1 && all.filter((w) => w.identifiers.doi === drag.identifiers.doi).length === 1);
      names["work_drag"] = drag.work_id;
      names["work_kers"] = kers.work_id;
      record({ id: "M1.2", via: "browser", request: { root: "papers" }, response: { works: all.length }, outcome: "ok", assertions: a,
        created: { work_drag: drag.work_id, work_kers: kers.work_id } });
    }

    // ----- M1.3: import selected Zotero items (read-only) -----------------------------------
    {
      const a: Assertion[] = [];
      const panel = page.getByTestId("zotero-panel");
      await expect(panel.getByTestId("zotero-status")).toHaveAttribute("data-state", "available");
      await panel.getByTestId("zotero-scope").selectOption("collection");
      await panel.getByTestId("zotero-collection").selectOption({ label: fx.zotero.collection });
      await panel.getByTestId("zotero-plan").click();
      const plan = panel.getByTestId("zotero-plan-result");
      await expect(plan).toBeVisible({ timeout: 60_000 });
      await expect(plan.locator("tbody tr")).toHaveCount(3);
      await expect(plan.locator('tr[data-key="DRAG0001"]')).toHaveAttribute("data-action", "merge");
      await plan.locator(`tr[data-key="${fx.zotero.deselect}"]`).getByTestId("zotero-pick").uncheck();
      await shot("M1.3-zotero-plan");
      await panel.getByTestId("zotero-commit").click();
      await expect(panel.getByTestId("zotero-commit-result")).toContainText("Imported 2 item(s)", { timeout: 60_000 });
      const all = await works();
      const holders = all.filter((w) => w.identifiers.doi === drag.identifiers.doi);
      bearing = all.find((w) => w.title === fx.titles.bearing)!;
      const shown = await get(`/library/works/${drag.work_id}`);
      const bases = new Set<string>(shown.record.body.field_provenance.map((p: { basis: string }) => p.basis));
      check(a, "the DOI the PDF, the BibTeX file and Zotero share is one work", holders.length === 1 && holders[0]?.work_id === drag.work_id);
      check(a, "that work holds the provenance of every source (the Zotero records beside the others)", bases.has("zotero") && bases.has("bibliography") && bases.size >= 3, [...bases]);
      check(a, "the Zotero-only article is an abstract-only work", bearing?.access_state === "abstract_only");
      check(a, "the deselected preprint was not imported", !all.some((w) => w.title === "Bearing drag in rarefied gas"));
      names["work_bearing"] = bearing.work_id;
      record({ id: "M1.3", via: "browser", request: { collection: fx.zotero.collection, deselected: fx.zotero.deselect }, response: { works: all.length, bases: [...bases].sort() },
        outcome: "ok", assertions: a, created: { work_bearing: bearing.work_id } });
    }

    // ----- M1.4: search the page text and read the page ----------------------------------------
    {
      const a: Assertion[] = [];
      await page.getByTestId("library-query").fill("drag coefficient");
      await page.getByTestId("library-search").click();
      const hitCard = page.getByTestId("library-hits").locator('li.card[data-page="2"]').filter({ hasText: "Aerodynamic" }).first();
      await expect(hitCard).toBeVisible();
      await hitCard.getByRole("button", { name: "Open page 2" }).click();
      await expect(page.getByTestId("library-page-text")).toHaveAttribute("data-page", "2");
      await expect(page.getByTestId("library-page-text")).toContainText(fx.sentence);
      await shot("M1.4-search-read");
      const found = await get("/library/search?query=drag%20coefficient&limit=20");
      check(a, "the search names the page and character span of the hit",
        found.hits.some((h: { work_id: string; page: number; start: number; end: number }) => h.work_id === drag.work_id && h.page === 2 && h.end > h.start));
      record({ id: "M1.4", via: "browser", request: { query: "drag coefficient" }, response: found, outcome: "ok", assertions: a });
    }

    // ----- M1.5: a matrix over three works (full text, full text, abstract only) -----------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge/matrix`);
      const create = page.getByTestId("matrix-create");
      await create.locator("summary").click();
      await create.getByTestId("matrix-create-title").fill("Flywheel losses");
      for (const [i, [label, kind, choices]] of fx.columns.entries()) {
        if (i > 0) await create.getByTestId("matrix-create-add-column").click();
        await create.getByTestId(`matrix-create-label-${i}`).fill(label);
        await create.getByTestId(`matrix-create-kind-${i}`).selectOption(kind);
        if (choices) await create.getByTestId(`matrix-create-choices-${i}`).fill(choices);
      }
      for (const w of [drag, kers, bearing]) await create.getByTestId(`matrix-create-work-${w.work_id}`).check();
      await create.getByTestId("matrix-create-submit").click();
      await expect(page.getByTestId("matrix-grid")).toBeVisible();
      const list = (await get("/matrix")).matrices as { matrix_id: string; title: string }[];
      matrixId = list[0]!.matrix_id;
      const v = await view(matrixId);
      check(a, "one matrix, titled as typed", list.length === 1 && list[0]!.title === "Flywheel losses");
      check(a, "its columns are the typed ones", JSON.stringify(v.matrix.columns.map((c) => c.id)) === JSON.stringify(["method", "pressure", "drag-coefficient", "key-finding"]));
      check(a, "one row per work, each showing the work's access state",
        JSON.stringify(v.rows.map((r) => r.access_state).sort()) === JSON.stringify(["abstract_only", "full_text", "full_text"]), v.rows.map((r) => r.access_state));
      names["matrix"] = matrixId;
      record({ id: "M1.5", via: "browser", request: { title: "Flywheel losses" }, response: v, outcome: "ok", assertions: a, created: { matrix: matrixId } });
    }

    // ----- M1.6: classify by rules ---------------------------------------------------------------
    {
      const a: Assertion[] = [];
      const rules = page.getByTestId("matrix-rules");
      await rules.locator("summary").click();
      await rules.getByTestId("matrix-rule-text-method").fill(fx.rules.method_text);
      await rules.getByTestId("matrix-rule-value-method").selectOption(fx.rules.method_value);
      await rules.getByTestId("matrix-rule-text-pressure").fill(fx.rules.pressure_text);
      await rules.getByTestId("matrix-rule-unit-pressure").fill(fx.rules.pressure_unit);
      await rules.getByTestId("matrix-rule-mode-key-finding").selectOption("pattern");
      await rules.getByTestId("matrix-rule-text-key-finding").fill(fx.rules.finding_text);
      await rules.getByTestId("matrix-rules-run").click();
      await expect(page.getByTestId("matrix-rules-result")).toContainText("created 3", { timeout: 60_000 });
      await shot("M1.6-rules");
      const v = await view(matrixId);
      const row = (id: string) => v.rows.find((r) => r.work_id === id);
      check(a, "the drag paper's method is classified by the rule, as proposed", cellOf(row(drag.work_id), "method")?.value_text === "experiment" && cellOf(row(drag.work_id), "method")?.state === "proposed");
      check(a, "its pressure is the quoted 10 Pa and its finding the quoted 92 %",
        cellOf(row(drag.work_id), "pressure")?.value_text === "10 Pa" && cellOf(row(drag.work_id), "key-finding")?.value_text === "reduces windage loss by 92 %");
      check(a, "the abstract-only Zotero work has no value claim: every cell is unsupported",
        new Set(row(bearing.work_id)?.cells.map((c) => c.state)).size === 1 && row(bearing.work_id)?.cells[0]?.state === "unsupported");
      record({ id: "M1.6", via: "browser", request: { rules: "method, pressure, key-finding" }, response: v, outcome: "ok", assertions: a });
    }

    // ----- M1.7: classify one work by hand: a choice label citing a selected sentence -----------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge/library`);
      await page.getByTestId("library-query").fill("kinetic energy recovery");
      await page.getByTestId("library-search").click();
      const card = page.getByTestId("library-hits").locator('li.card[data-page="1"]').filter({ hasText: "Kinetic" }).first();
      await card.getByRole("button", { name: "Open page 1" }).click();
      await selectInReader(page, fx.review_sentence);
      await page.getByTestId("library-cite-selection").click();
      const cite = page.getByTestId("cite-in-matrix");
      await expect(cite).toContainText(fx.review_sentence);
      await cite.getByTestId("cite-column").selectOption("method");
      await cite.getByTestId("cite-value").selectOption("review");
      await cite.getByTestId("cite-submit").click();
      await expect(cite.getByTestId("cite-done")).toBeVisible();
      const v = await view(matrixId);
      const cell = cellOf(v.rows.find((r) => r.work_id === kers.work_id), "method");
      check(a, "the review paper is labelled 'review', proposed, citing page 1", cell?.value_text === "review" && cell?.state === "proposed" && cell?.evidence[0]?.where.page === 1, cell);
      record({ id: "M1.7", via: "browser", request: { work: "kers", column: "method", value: "review" }, response: cell ?? {}, outcome: "ok", assertions: a });
    }

    // ----- M1.8: cite the drag coefficient; two misquotes are refused verbatim ---------------
    {
      const a: Assertion[] = [];
      await page.getByTestId("library-query").fill("drag coefficient");
      await page.getByTestId("library-search").click();
      const card = page.getByTestId("library-hits").locator('li.card[data-page="2"]').filter({ hasText: "Aerodynamic" }).first();
      await card.getByTestId("library-cite").click();
      let cite = page.getByTestId("cite-in-matrix");
      await cite.getByTestId("cite-column").selectOption("drag-coefficient");
      await cite.getByTestId("cite-value").fill("0.0042");
      await cite.getByTestId("cite-decimal").fill("0.0042");
      await cite.getByTestId("cite-submit").click();
      await expect(cite.getByTestId("cite-refusal")).toContainText("does not occur verbatim");
      const first = (await cite.getByTestId("cite-refusal").innerText()).trim();
      check(a, "the search hit quotes only 'drag coefficient': the number is refused, verbatim", first.includes("0.0042 does not occur verbatim in the quoted evidence"), first);
      await card.getByRole("button", { name: "Open page 2" }).click();
      await selectInReader(page, fx.sentence);
      await page.getByTestId("library-cite-selection").click();
      cite = page.getByTestId("cite-in-matrix");
      await expect(cite).toContainText(fx.sentence); // the panel of the selection has replaced the old one
      await cite.getByTestId("cite-column").selectOption("drag-coefficient");
      await cite.getByTestId("cite-value").fill("0.042");
      await cite.getByTestId("cite-decimal").fill("0.042");
      await cite.getByTestId("cite-submit").click();
      await expect(cite.getByTestId("cite-refusal")).toContainText("0.042 does not occur verbatim");
      const second = (await cite.getByTestId("cite-refusal").innerText()).trim();
      check(a, "the sentence is real but 0.042 is not in it: refused, verbatim", second.includes("cell drag-coefficient: 0.042 does not occur verbatim"), second);
      await shot("M1.8-misquote");
      await cite.getByTestId("cite-value").fill("0.0042");
      await cite.getByTestId("cite-decimal").fill("0.0042");
      await cite.getByTestId("cite-submit").click();
      await expect(cite.getByTestId("cite-done")).toBeVisible();
      const v = await view(matrixId);
      const cell = cellOf(v.rows.find((r) => r.work_id === drag.work_id), "drag-coefficient");
      const page2 = await get(`/library/works/${drag.work_id}/read?page=2&max_chars=8000`);
      const at = (page2.text as string).indexOf(fx.sentence);
      check(a, "the cell binds 0.0042 to the page, the span and the quote digest",
        cell?.value_text === "0.0042" && cell?.state === "proposed" && cell?.evidence[0]?.where.page === 2 && cell?.evidence[0]?.where.start === at && cell?.evidence[0]?.where.end === at + fx.sentence.length, cell);
      record({ id: "M1.8", via: "browser", request: { column: "drag-coefficient", attempts: ["0.0042 on a hit", "0.042", "0.0042"] },
        response: { refused: [first, second], cell }, outcome: "ok", assertions: a });
    }

    // ----- M1.9: review the cited cell -------------------------------------------------------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge/matrix`);
      await page.getByTestId(`matrix-open-${matrixId}`).click();
      await page.getByTestId(`matrix-review-${drag.work_id}-drag-coefficient`).click();
      await expect(page.getByTestId(`matrix-cell-${drag.work_id}-drag-coefficient`)).toHaveAttribute("data-state", "reviewed");
      await shot("M1.9-reviewed");
      const v = await view(matrixId);
      const cell = cellOf(v.rows.find((r) => r.work_id === drag.work_id), "drag-coefficient");
      check(a, "the owner's review is stored: the cell is reviewed", cell?.state === "reviewed", cell?.state);
      check(a, "the other cells were not reviewed with it", cellOf(v.rows.find((r) => r.work_id === drag.work_id), "method")?.state === "proposed");
      record({ id: "M1.9", via: "browser", request: { cell: "drag-coefficient" }, response: cell ?? {}, outcome: "ok", assertions: a });
    }
  } catch (error) {
    await shot("FAILED-owner-1").catch(() => undefined);
    throw error;
  } finally {
    save();
  }

  // ----- The agent's session (MCP, run by the harness while this hub keeps serving) -------------
  writeFileSync(join(signals, "owner-1.done"), "done", "utf-8");
  const agent = await waitForAgent();
  const correctionId = agent["correction"] ?? "";

  try {
    // ----- M2.1: export the brief -------------------------------------------------------------------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge/matrix`);
      await page.getByTestId(`matrix-open-${matrixId}`).click();
      await page.getByTestId("matrix-export-format").selectOption("markdown");
      await page.getByTestId("matrix-export-run").click();
      await expect(page.getByTestId("matrix-export-brief")).toBeVisible();
      await shot("M2.1-brief");
      const v = await view(matrixId);
      check(a, "one brief is registered for the matrix, current", v.briefs.length === 1 && !v.briefs[0]!.needs_review, v.briefs);
      names["brief"] = v.briefs[0]?.version_id ?? "";
      record({ id: "M2.1", via: "browser", request: { format: "markdown" }, response: v.briefs, outcome: "ok", assertions: a, created: { brief: names["brief"] } });
    }

    // ----- M2.2: apply the agent's correction in the hub: the row and the brief are flagged -------
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/corrections/${correctionId}`);
      const shown = page.getByTestId("correction");
      await expect(shown).toHaveAttribute("data-status", "proposed");
      await page.getByRole("button", { name: "Preview applying this correction" }).click();
      await expect(page.getByTestId("affected-records")).toContainText("will be marked Superseded");
      await page.getByTestId("decision-preview").getByRole("button", { name: "Approve correction" }).click();
      await expect(shown).toHaveAttribute("data-status", "applied");
      const v = await view(matrixId);
      const row = v.rows.find((r) => r.work_id === drag.work_id);
      check(a, "the drag paper's row needs review", row?.needs_review === true);
      check(a, "the brief is flagged", v.briefs.length === 1 && v.briefs[0]!.needs_review === true);
      await page.goto(`${origin}/knowledge/matrix`);
      await page.getByTestId(`matrix-open-${matrixId}`).click();
      await expect(page.getByTestId(`matrix-needs-review-${drag.work_id}`)).toBeVisible();
      await shot("M2.2-needs-review");
      record({ id: "M2.2", via: "browser", request: { correction: "correction" }, response: { row: row?.needs_review, briefs: v.briefs }, outcome: "ok", assertions: a });
    }

    // ----- M2.3: re-review the finding that relied on the superseded assumption -------------------
    {
      const a: Assertion[] = [];
      await page.getByTestId(`matrix-review-${drag.work_id}-key-finding`).click();
      await expect(page.getByTestId(`matrix-row-${drag.work_id}`)).toHaveAttribute("data-needs-review", "no");
      await shot("M2.3-cleared");
      const v = await view(matrixId);
      const row = v.rows.find((r) => r.work_id === drag.work_id);
      check(a, "the row's flag is cleared", row?.needs_review === false);
      check(a, "the finding is reviewed with the agent's value", cellOf(row, "key-finding")?.value_text === fx.losses && cellOf(row, "key-finding")?.state === "reviewed", cellOf(row, "key-finding"));
      check(a, "the brief stays flagged until it is exported again", v.briefs.every((b) => b.needs_review));
      record({ id: "M2.3", via: "browser", request: { cell: "key-finding" }, response: cellOf(row, "key-finding") ?? {}, outcome: "ok", assertions: a });
    }
  } catch (error) {
    await shot("FAILED-owner-2").catch(() => undefined);
    throw error;
  } finally {
    save();
    await owner.dispose().catch(() => undefined);
    await context.close().catch(() => undefined);
  }
  const failed = steps.flatMap((s) => s.assertions.filter((x) => !x.passed).map((x) => `${s.id}: ${x.claim}`));
  expect(failed).toEqual([]);
});
