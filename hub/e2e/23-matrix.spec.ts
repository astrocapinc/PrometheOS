// A02, CP11 S11.4 (decision 0064): the evidence matrix under Knowledge. A matrix "Flywheel losses"
// over the synthetic drag paper (full text) and the scan without text (metadata only) gets a
// quoted quantity, a rule run and an unsupported cell through the API; in the hub the owner sees
// the grid with state, basis and access-state badges, opens a cell's evidence on its page,
// reviews cells, marks one unsupported and exports the brief; an applied correction that
// supersedes an assumption the row relies on flags the row and the brief. Every outcome is
// checked against stored state through the API; screenshots supplement the assertions.
import { createHash } from "node:crypto";
import { expect, shot, test, type Api } from "./fixtures";

type Work = { work_id: string; title: string; access_state: string; page_count: number | null };
type Cell = { column_id: string; value_text: string; state: string; basis: string; evidence: { where: { page?: number; start?: number; end?: number } }[] };
type Row = { work_id: string; access_state: string; extraction_id: string | null; needs_review: boolean; cells: Cell[] };
type View = { matrix: { matrix_id: string }; rows: Row[]; briefs: { version_id: string; needs_review: boolean }[] };

const COLUMNS = [
  { id: "method", label: "Method", value_kind: "choice", instructions: "", choices: ["experiment", "simulation", "review"] },
  { id: "pressure", label: "Pressure", value_kind: "quantity", instructions: "" },
  { id: "drag", label: "Drag coefficient", value_kind: "quantity", instructions: "" },
  { id: "windage", label: "Windage loss", value_kind: "quantity", instructions: "" },
  { id: "finding", label: "Key finding", value_kind: "text", instructions: "" },
];
const SENTENCE = "The drag coefficient was 0.0042 at 10 Pa.";

function create(kind: string, id: string, body: unknown) {
  return { op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body };
}

function assumption(title: string, text: string) {
  return {
    title, text, category: "assumption", evidence: [], claim_refs: [], author_ref: null,
    origin: "typed", quantities: [], review: "unreviewed",
  };
}

async function ownerApply(api: Api, operations: unknown[], protectedRequest = false) {
  const request = {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations,
  };
  if (!protectedRequest) return api.post("/transactions", request);
  const preview = await api.post<{ digest: string }>("/decisions/preview", { request });
  return api.post("/decisions/apply", { request, digest: preview.digest });
}

test("the owner reviews grounded cells, opens evidence, exports the brief and sees a correction flag", async ({ paired: page, hub, api }) => {
  let works = (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  if (!works.some((w) => w.page_count !== null)) {
    const planned = await api.post<{ result: { plan_id: string } }>("/library/plans", { root_alias: "research", path: "library", wait_seconds: 20 });
    await api.post(`/library/plans/${planned.result.plan_id}/commit`, { wait_seconds: 20 });
    works = (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  }
  // The bibliography journey (22) may have run first; import the bibliography files when not.
  if (!works.some((w) => w.title === "Bearing losses in vacuum")) {
    const planned = await api.post<{ result: { plan_id: string } }>("/library/plans", { root_alias: "research", path: "bibliography", wait_seconds: 20 });
    await api.post(`/library/plans/${planned.result.plan_id}/commit`, { wait_seconds: 20 });
    works = (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  }
  const drag = works.find((w) => w.title.startsWith("Aerodynamic"))!;
  const scan = works.find((w) => w.access_state === "metadata_only" && w.page_count !== null)!;
  const bearing = works.find((w) => w.title === "Bearing losses in vacuum")!;
  expect(drag && scan && bearing).toBeTruthy();

  const made = await api.post<{ matrix_id: string }>("/matrix", { title: "Flywheel losses", columns: COLUMNS, work_ids: [drag.work_id, scan.work_id, bearing.work_id] });
  const matrixId = made.matrix_id;
  const base = `/matrix/${matrixId}`;
  // A quoted quantity (the exact sentence on page 2), a rule run, an unsupported metadata-only cell.
  const page2 = await api.get<{ text: string }>(`/library/works/${drag.work_id}/read?page=2&max_chars=8000`);
  const at = page2.text.indexOf(SENTENCE);
  const read = await api.get<{ evidence: unknown }>(`/library/works/${drag.work_id}/read?page=2&start=${at}&end=${at + SENTENCE.length}`);
  await api.post(`${base}/propose`, {
    work_id: drag.work_id, column_id: "drag", value_text: "0.0042 at 10 Pa", evidence: [read.evidence],
    quantities: [{ decimal: "0.0042", unit: "" }, { decimal: "10", unit: "Pa" }],
  });
  const misquote = await api.post<{ error: { code: string; message: string } }>(
    `${base}/propose`,
    { work_id: drag.work_id, column_id: "drag", value_text: "0.042", evidence: [read.evidence], quantities: [{ decimal: "0.042", unit: "" }] },
    400,
  );
  expect(misquote.error.code).toBe("validation_error");
  expect(misquote.error.message).toContain("0.042 does not occur verbatim");
  await api.post(`${base}/extract`, {
    rules: { rules: [
      { id: "experiment", column: "method", keywords: ["was evacuated"], value: "experiment" },
      { id: "pressure", column: "pressure", pattern: "evacuated to {value}", unit: "Pa" },
    ] },
  });
  await api.post(`${base}/unsupported`, { work_id: scan.work_id, column_id: "windage", reason: "metadata only: no text to quote" });
  // A metadata cell quoting the work's own bibliography lines (its title field).
  const record = (await api.get<{ record: { body: { field_provenance: { field: string; basis: string; version_ref: { id: string }; locator: { start_line: number; end_line: number } }[] } } }>(
    `/library/works/${bearing.work_id}`,
  )).record.body.field_provenance.find((r) => r.basis === "bibliography" && r.field === "title")!;
  const bibText = (await api.get<{ text: string }>(`/versions/${record.version_ref.id}/text?limit=100000`)).text;
  const bibVersion = await api.get<{ body: { source_id: string } }>(`/records/${record.version_ref.id}`);
  const quotedLines = bibText.split("\n").slice(record.locator.start_line - 1, record.locator.end_line).map((l) => l.replace(/\r$/, "")).join("\n");
  await api.post(`${base}/propose`, {
    work_id: bearing.work_id, column_id: "finding", value_text: "Bearing losses in vacuum", basis: "metadata",
    evidence: [{
      source_id: bibVersion.body.source_id, version_id: record.version_ref.id,
      locator: { type: "text", start_line: record.locator.start_line, end_line: record.locator.end_line },
      quote_sha256: createHash("sha256").update(quotedLines, "utf8").digest("hex"),
    }],
  });

  await page.goto(`${hub.url}knowledge`);
  await page.getByTestId("knowledge-matrix-link").getByRole("link").click();
  await expect(page.getByRole("heading", { name: "Evidence matrix", level: 1 })).toBeVisible();
  await page.getByTestId(`matrix-open-${matrixId}`).click();
  const grid = page.getByTestId("matrix-grid");
  await expect(grid).toBeVisible();
  const dragRow = page.getByTestId(`matrix-row-${drag.work_id}`);
  await expect(dragRow).toHaveAttribute("data-access", "full_text");
  await expect(page.getByTestId(`matrix-row-${scan.work_id}`)).toHaveAttribute("data-access", "metadata_only");
  await expect(page.getByTestId(`matrix-row-${scan.work_id}`)).toContainText("Metadata only");
  const coefficient = page.getByTestId(`matrix-cell-${drag.work_id}-drag`);
  await expect(coefficient).toHaveAttribute("data-state", "proposed");
  await expect(coefficient).toHaveAttribute("data-basis", "full_text");
  await expect(coefficient).toContainText("0.0042 at 10 Pa");
  await expect(page.getByTestId(`matrix-cell-${scan.work_id}-windage`)).toHaveAttribute("data-state", "unsupported");
  await shot(page, "matrix-grid");

  // The evidence opens the page reader at the quoted span.
  await page.getByTestId(`matrix-evidence-${drag.work_id}-drag-0`).click();
  const reader = page.getByTestId("library-page-text");
  await expect(reader).toHaveAttribute("data-page", "2");
  await expect(reader.locator("mark")).toHaveText(SENTENCE);
  await shot(page, "matrix-evidence");

  // The owner reviews two cells (an owner decision in this paired session).
  // Line evidence (a bibliography entry) opens the quoted lines (review F10).
  const metadataCell = page.getByTestId(`matrix-cell-${bearing.work_id}-finding`);
  await expect(metadataCell).toHaveAttribute("data-basis", "metadata");
  await page.getByTestId(`matrix-evidence-${bearing.work_id}-finding-0`).click();
  await expect(page.getByTestId("matrix-lines-text")).toHaveText(quotedLines);
  await expect(page.getByTestId("matrix-lines-text")).toContainText("Bearing losses in vacuum");
  await shot(page, "matrix-lines");

  await page.getByTestId(`matrix-review-${drag.work_id}-drag`).click();
  await expect(coefficient).toHaveAttribute("data-state", "reviewed");
  await page.getByTestId(`matrix-review-${drag.work_id}-pressure`).click();
  await expect(page.getByTestId(`matrix-cell-${drag.work_id}-pressure`)).toHaveAttribute("data-state", "reviewed");
  let view = await api.get<View>(base);
  const stored = view.rows.find((r) => r.work_id === drag.work_id)!;
  expect(Object.fromEntries(stored.cells.map((c) => [c.column_id, c.state]))).toEqual({
    method: "proposed", pressure: "reviewed", drag: "reviewed",
  });
  // Mark unsupported asks for the reason; the stored cell claims no value.
  await page.getByTestId(`matrix-unsupported-${drag.work_id}-method`).click();
  await page.getByTestId("matrix-unsupported-reason").fill("the method is not stated");
  await page.getByTestId("matrix-unsupported-confirm").click();
  await expect(page.getByTestId(`matrix-cell-${drag.work_id}-method`)).toHaveAttribute("data-state", "unsupported");
  view = await api.get<View>(base);
  const method = view.rows.find((r) => r.work_id === drag.work_id)!.cells.find((c) => c.column_id === "method")!;
  expect(method).toMatchObject({ state: "unsupported", value_text: "not found: the method is not stated", evidence: [] });

  // The brief: registered, the same content the API stored.
  await page.getByTestId("matrix-export-format").selectOption("markdown");
  await page.getByTestId("matrix-export-run").click();
  await expect(page.getByTestId("matrix-export-brief")).toBeVisible();
  const content = await page.getByTestId("matrix-export-content").inputValue();
  expect(content).toContain("# Brief: Flywheel losses");
  expect(content).toContain("access: metadata_only");
  view = await api.get<View>(base);
  expect(view.briefs).toHaveLength(1);
  const brief = view.briefs[0]!.version_id;
  const storedText = await api.get<{ text: string }>(`/versions/${brief}/text?limit=100000`);
  expect(storedText.text).toBe(content);
  await shot(page, "matrix-brief");

  // A correction superseding an assumption the row relies on flags the row and the brief.
  const relied = `entry_${crypto.randomUUID()}`;
  const replacement = `entry_${crypto.randomUUID()}`;
  await ownerApply(api, [create("entry", relied, assumption("Windage scales with p^0.8", "Assume p to 0.8."))]);
  const loss = "Losses scale with p^0.8.";
  const lossAt = page2.text.indexOf(loss);
  const lossRead = await api.get<{ evidence: unknown }>(`/library/works/${drag.work_id}/read?page=2&start=${lossAt}&end=${lossAt + loss.length}`);
  await api.post(`${base}/propose`, {
    work_id: drag.work_id, column_id: "finding", value_text: loss, evidence: [lossRead.evidence], relies_on: [relied],
  });
  await api.post(`${base}/export`, { format: "markdown" });
  await ownerApply(api, [create("entry", replacement, assumption("Windage scales with p^0.9", "Assume p to 0.9."))]);
  const correction = `correction_${crypto.randomUUID()}`;
  await ownerApply(api, [
    create("correction", correction, {
      target_refs: [{ id: relied, rev: 1 }], replacement_refs: [{ id: replacement, rev: 1 }],
      reason: "the loss model was refitted", evidence: [],
      effects: [{ target_ref: { id: relied, rev: 1 }, effect: "supersede", replacement_ref: { id: replacement, rev: 1 }, replacement_text: null }],
      status: "proposed", applied_seq: null,
    }),
  ]);
  await ownerApply(api, [{ op: "correction_apply", id: correction, base_rev: 1 }], true);
  view = await api.get<View>(base);
  expect(view.rows.find((r) => r.work_id === drag.work_id)!.needs_review).toBe(true);
  expect(view.briefs.every((b) => b.needs_review)).toBe(true);
  await page.reload();
  await page.getByTestId(`matrix-open-${matrixId}`).click();
  await expect(page.getByTestId(`matrix-needs-review-${drag.work_id}`)).toBeVisible();
  await expect(dragRow).toHaveAttribute("data-needs-review", "yes");
  await expect(page.getByTestId("matrix-briefs").locator('li[data-needs-review="yes"]')).toHaveCount(2);
  await shot(page, "matrix-needs-review");
  // The owner's re-review clears the row's flag.
  await page.getByTestId(`matrix-review-row-${drag.work_id}`).click();
  await expect(dragRow).toHaveAttribute("data-needs-review", "no");
  view = await api.get<View>(base);
  expect(view.rows.find((r) => r.work_id === drag.work_id)!.needs_review).toBe(false);
});
