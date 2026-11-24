// A02, CP11 S11.4b (decision 0066 §3): the whole CP11-so-far flow in the hub, on a project that
// starts with NO research folder (the fixture's third hub). The owner adds the papers folder,
// imports PDFs and bibliographies, searches, reads a page, keeps a look-alike pair separate,
// creates a matrix, runs rules, cites spans into cells (a misquote refused verbatim), reviews,
// exports the brief, applies a correction an agent proposed (the row and the brief flagged) and
// re-reviews the cell that relied on it. Every step asserts the stored state through a separately
// paired owner client; screenshots supplement the assertions.
import { request as pwRequest, type APIRequestContext, type Page } from "@playwright/test";
import { expect, readHub, shot, test } from "./fixtures";

interface FlowInfo {
  url: string;
  project_id: string;
  papers: string;
  notes: string;
  hidden: string;
  pairing_tokens: string[];
  agent_token: string;
}

type Work = { work_id: string; title: string; access_state: string; page_count: number | null };
type Cell = { column_id: string; value_text: string; state: string; evidence: { where: { page?: number; start?: number; end?: number } }[] };
type Row = { work_id: string; needs_review: boolean; cells: Cell[] };
type View = { matrix: { matrix_id: string; columns: { id: string }[] }; rows: Row[]; briefs: { version_id: string; needs_review: boolean }[] };

const SENTENCE = "The drag coefficient was 0.0042 at 10 Pa.";
const LOSSES = "Losses scale with p^0.8.";

function flowInfo(): FlowInfo {
  const info = readHub() as unknown as { owner_flow?: FlowInfo };
  if (!info.owner_flow) throw new Error("the hub fixture has no owner-flow hub (tools/hub_fixture.py)");
  return info.owner_flow;
}

const origin = (f: FlowInfo) => f.url.replace(/\/$/, "");

class Owner {
  constructor(
    readonly ctx: APIRequestContext,
    readonly csrf: string,
  ) {}

  async get<T = any>(path: string): Promise<T> {
    const reply = await this.ctx.get(`/api/v1${path}`);
    expect(reply.status(), `${path}: ${await reply.text()}`).toBe(200);
    return (await reply.json()) as T;
  }
}

async function ownerClient(f: FlowInfo, token: string): Promise<Owner> {
  const ctx = await pwRequest.newContext({ baseURL: origin(f), extraHTTPHeaders: { Origin: origin(f) } });
  const reply = await ctx.post("/api/v1/pair", { data: { token } });
  expect(reply.status(), await reply.text()).toBe(200);
  return new Owner(ctx, ((await reply.json()) as { csrf_token: string }).csrf_token);
}

async function agentPost<T = any>(agent: APIRequestContext, path: string, body: unknown, status = 200): Promise<T> {
  const reply = await agent.post(`/api/v1${path}`, { data: body });
  expect(reply.status(), `${path}: ${await reply.text()}`).toBe(status);
  return (await reply.json()) as T;
}

async function pairPage(page: Page, f: FlowInfo, token: string): Promise<void> {
  const reply = await page.request.post(`${origin(f)}/api/v1/pair`, { data: { token }, headers: { Origin: origin(f) } });
  expect(reply.status(), await reply.text()).toBe(200);
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

test("CP11 so far, in the hub: from no research folder to a reviewed, corrected and re-reviewed matrix", async ({ page }) => {
  test.setTimeout(240_000);
  const f = flowInfo();
  const owner = await ownerClient(f, f.pairing_tokens[1]!);
  const agent = await pwRequest.newContext({ baseURL: origin(f), extraHTTPHeaders: { Authorization: `Bearer ${f.agent_token}` } });
  await pairPage(page, f, f.pairing_tokens[0]!);
  try {
    // 1. Add the folder. The project starts with none.
    expect((await owner.get<{ roots: unknown[] }>("/project/roots")).roots).toEqual([]);
    const before = await owner.get<{ settings_rev: number }>("/project");
    await page.goto(`${f.url}knowledge/library`);
    await expect(page.getByTestId("library-no-roots")).toContainText("Research folders below");
    const panel = page.getByTestId("research-folders");
    await expect(panel.getByTestId("research-folders-removal")).toContainText("cannot be removed");
    await panel.getByTestId("research-folder-alias").fill("papers");
    await panel.getByTestId("research-folder-path").fill(f.papers);
    await shot(page, "owner-flow-add-folder");
    await panel.getByTestId("research-folder-add").click();
    // The page now has a folder: the import appears and the panel (collapsed) counts it.
    await expect(page.getByTestId("library-plan")).toBeVisible();
    await expect(page.getByTestId("research-folders").locator("summary")).toContainText("(1)");
    const roots = await owner.get<{ roots: { alias: string; path: string; available: boolean }[] }>("/project/roots");
    // (the hub never returns local paths: the folder is named by its alias)
    expect(roots.roots).toEqual([{ alias: "papers", available: true }]);
    const after = await owner.get<{ settings_rev: number; settings: { allowed_roots: { alias: string }[] } }>("/project");
    expect(after.settings_rev).toBe(before.settings_rev + 1); // an audited settings revision
    expect(after.settings.allowed_roots.map((r) => r.alias)).toEqual(["papers"]);

    // 1b. An agent asks for two more folders (approval_required, nothing changes). The owner
    // reviews them on the Approvals page, where each folder is named without a local path: a
    // base placeholder and the folders below it, with warnings (review round 1, F1). The owner
    // grants the ordinary one (the agent resumes it) and denies the flagged one.
    type Asked = { error: { code: string; details: { approval_request_id: string } } };
    const askNotes = await agentPost<Asked>(agent, "/project/roots", { alias: "notes", path: f.notes }, 403);
    const askKeys = await agentPost<Asked>(agent, "/project/roots", { alias: "keys", path: f.hidden }, 403);
    expect([askNotes.error.code, askKeys.error.code]).toEqual(["approval_required", "approval_required"]);
    expect((await owner.get<{ roots: { alias: string }[] }>("/project/roots")).roots.map((r) => r.alias)).toEqual(["papers"]);
    await page.goto(`${f.url}approvals`);
    const cardOf = (id: string) => page.locator(`[data-testid="approval-pending"][data-approval-id="${id}"]`);
    const notesCard = cardOf(askNotes.error.details.approval_request_id);
    const keysCard = cardOf(askKeys.error.details.approval_request_id);
    await expect(notesCard.getByTestId("approval-summary")).toContainText("add the research folder notes: ");
    const notesLine = await notesCard.getByTestId("approval-summary").innerText();
    expect(notesLine.trim()).toMatch(/ > owner-flow > notes$/);
    expect(notesLine).not.toMatch(/[A-Za-z]:[\\/]|<user home>|<local path>/); // no path, no blind label
    expect((await notesCard.getByTestId("approval-folder-segment").allInnerTexts()).slice(-2)).toEqual(["owner-flow", "notes"]);
    await expect(notesCard.getByTestId("approval-folder-base")).not.toHaveText("");
    await expect(notesCard.getByTestId("approval-folder-warning")).toHaveCount(0);
    await expect(keysCard.getByTestId("approval-folder-warning")).toHaveText(["hidden folder: .api-keys", "sensitive name: .api-keys"]);
    await expect(keysCard.getByTestId("approval-summary")).toContainText("warning: hidden folder: .api-keys");
    await shot(page, "owner-flow-folder-requests");
    await notesCard.getByTestId("approval-review").click();
    await expect(notesCard.getByTestId("approval-folder-check")).toBeVisible();
    await notesCard.getByTestId("approval-grant").click();
    await expect(notesCard).toHaveCount(0);
    await keysCard.getByTestId("approval-deny").click();
    await expect(keysCard).toHaveCount(0);
    const resumed = await agentPost<{ state: string }>(agent, `/approvals/${askNotes.error.details.approval_request_id}/resume`, {});
    expect(resumed.state).toBe("used");
    const granted = await owner.get<{ settings_rev: number; settings: { allowed_roots: { alias: string }[] } }>("/project");
    expect(granted.settings.allowed_roots.map((r) => r.alias)).toEqual(["papers", "notes"]);
    expect(granted.settings_rev).toBe(before.settings_rev + 2);
    const keysState = await owner.get<{ state: string }>(`/approvals/${askKeys.error.details.approval_request_id}`);
    expect(keysState.state).toBe("denied");
    await page.goto(`${f.url}knowledge/library`);

    // 2. Import the PDFs and the bibliographies.
    await page.getByTestId("library-root").selectOption("papers");
    await page.getByTestId("library-plan").click();
    await expect(page.getByTestId("library-plan-result")).toBeVisible({ timeout: 60_000 });
    await page.getByTestId("library-commit").click();
    await expect(page.getByTestId("library-commit-result")).toBeVisible({ timeout: 90_000 });
    const works = (await owner.get<{ works: Work[] }>("/library/works?limit=200")).works;
    const drag = works.find((w) => w.title.startsWith("Aerodynamic"))!;
    const kers = works.find((w) => w.title.startsWith("Kinetic"))!;
    const bearing = works.find((w) => w.title === "Bearing losses in vacuum")!;
    expect(drag.page_count).toBe(4);
    expect(bearing.access_state).toBe("metadata_only");
    expect(kers.access_state).toBe("full_text");

    // 3, 4. Search and read a page.
    await page.getByTestId("library-query").fill("drag coefficient");
    await page.getByTestId("library-search").click();
    const hitCard = page.getByTestId("library-hits").locator('li.card[data-page="2"]').filter({ hasText: "Aerodynamic" }).first();
    await expect(hitCard).toBeVisible();
    await hitCard.getByRole("button", { name: "Open page 2" }).click();
    await expect(page.getByTestId("library-page-text")).toHaveAttribute("data-page", "2");
    await expect(page.getByTestId("library-page-text")).toContainText(SENTENCE);

    // 5. Keep a look-alike pair separate (an owner decision).
    const cards = page.getByTestId("library-candidate");
    const pair = cards.filter({ hasText: "Windage in sealed housings: a survey" });
    await pair.getByTestId("library-distinct").click();
    await pair.getByTestId("library-confirm-yes").click();
    await expect(pair).toHaveCount(0);
    const dedup = await owner.get<{ decisions: { choice: string }[] }>("/library/dedup");
    expect(dedup.decisions.map((d) => d.choice)).toContain("distinct");

    // 6. Create a matrix in the hub.
    await page.goto(`${f.url}knowledge/matrix`);
    const create = page.getByTestId("matrix-create");
    await create.locator("summary").click();
    await create.getByTestId("matrix-create-title").fill("Flywheel losses");
    const columns: [string, string, string?][] = [
      ["Method", "choice", "experiment, simulation, review"],
      ["Pressure", "quantity"],
      ["Drag coefficient", "quantity"],
      ["Key finding", "text"],
    ];
    for (const [i, [label, kind, choices]] of columns.entries()) {
      if (i > 0) await create.getByTestId("matrix-create-add-column").click();
      await create.getByTestId(`matrix-create-label-${i}`).fill(label);
      await create.getByTestId(`matrix-create-kind-${i}`).selectOption(kind);
      if (choices) await create.getByTestId(`matrix-create-choices-${i}`).fill(choices);
    }
    for (const w of [drag, kers, bearing]) await create.getByTestId(`matrix-create-work-${w.work_id}`).check();
    await shot(page, "owner-flow-create-matrix");
    await create.getByTestId("matrix-create-submit").click();
    await expect(page.getByTestId("matrix-grid")).toBeVisible();
    const list = await owner.get<{ matrices: { matrix_id: string; title: string }[] }>("/matrix");
    expect(list.matrices.map((m) => m.title)).toEqual(["Flywheel losses"]);
    const matrixId = list.matrices[0]!.matrix_id;
    let view = await owner.get<View>(`/matrix/${matrixId}`);
    expect(view.matrix.columns.map((c) => c.id)).toEqual(["method", "pressure", "drag-coefficient", "key-finding"]);
    expect(view.rows.map((r) => r.work_id).sort()).toEqual([drag.work_id, kers.work_id, bearing.work_id].sort());

    // 7. Run rules per column.
    const rules = page.getByTestId("matrix-rules");
    await rules.locator("summary").click();
    await rules.getByTestId("matrix-rule-text-method").fill("was evacuated");
    await rules.getByTestId("matrix-rule-value-method").selectOption("experiment");
    await rules.getByTestId("matrix-rule-text-pressure").fill("evacuated to {value}");
    await rules.getByTestId("matrix-rule-unit-pressure").fill("Pa");
    await rules.getByTestId("matrix-rule-mode-key-finding").selectOption("pattern");
    await rules.getByTestId("matrix-rule-text-key-finding").fill("reduces windage loss by {value} %");
    await rules.getByTestId("matrix-rules-run").click();
    await expect(page.getByTestId("matrix-rules-result")).toContainText("created 3", { timeout: 60_000 });
    await shot(page, "owner-flow-rules");
    view = await owner.get<View>(`/matrix/${matrixId}`);
    const dragRow = () => view.rows.find((r) => r.work_id === drag.work_id)!;
    const cellOf = (row: Row, id: string) => row.cells.find((c) => c.column_id === id);
    expect(cellOf(dragRow(), "method")).toMatchObject({ value_text: "experiment", state: "proposed" });
    expect(cellOf(dragRow(), "pressure")).toMatchObject({ value_text: "10 Pa", state: "proposed" });
    expect(cellOf(dragRow(), "key-finding")).toMatchObject({ value_text: "reduces windage loss by 92 %", state: "proposed" });
    const bearingRow = view.rows.find((r) => r.work_id === bearing.work_id)!;
    expect(new Set(bearingRow.cells.map((c) => c.state))).toEqual(new Set(["unsupported"]));

    // 8. Cite spans into the drag coefficient cell: a misquote is refused verbatim.
    await page.goto(`${f.url}knowledge/library`);
    await page.getByTestId("library-query").fill("drag coefficient");
    await page.getByTestId("library-search").click();
    const card = page.getByTestId("library-hits").locator('li.card[data-page="2"]').filter({ hasText: "Aerodynamic" }).first();
    await card.getByTestId("library-cite").click();
    let cite = page.getByTestId("cite-in-matrix");
    await cite.getByTestId("cite-column").selectOption("drag-coefficient");
    await cite.getByTestId("cite-value").fill("0.0042");
    await cite.getByTestId("cite-decimal").fill("0.0042");
    await cite.getByTestId("cite-submit").click();
    // The hit quotes only "drag coefficient": the number is not in that span.
    await expect(cite.getByTestId("cite-refusal")).toContainText("0.0042 does not occur verbatim in the quoted evidence");
    expect(cellOf((await owner.get<View>(`/matrix/${matrixId}`)).rows.find((r) => r.work_id === drag.work_id)!, "drag-coefficient")).toBeUndefined();
    await card.getByRole("button", { name: "Open page 2" }).click();
    await selectInReader(page, SENTENCE);
    await page.getByTestId("library-cite-selection").click();
    cite = page.getByTestId("cite-in-matrix");
    await expect(cite).toContainText(SENTENCE);
    // A new column starts clean (review F7): a choice column offers its first choice; moving to a
    // quantity column puts the quote back and clears the number.
    await cite.getByTestId("cite-column").selectOption("method");
    await expect(cite.getByTestId("cite-value")).toHaveValue("experiment");
    await cite.getByTestId("cite-column").selectOption("pressure");
    await cite.getByTestId("cite-decimal").fill("10");
    await cite.getByTestId("cite-column").selectOption("drag-coefficient");
    await expect(cite.getByTestId("cite-value")).toHaveValue(SENTENCE);
    await expect(cite.getByTestId("cite-decimal")).toHaveValue("");
    await cite.getByTestId("cite-value").fill("0.042");
    await cite.getByTestId("cite-decimal").fill("0.042");
    await cite.getByTestId("cite-submit").click();
    await expect(cite.getByTestId("cite-refusal")).toContainText("cell drag-coefficient: 0.042 does not occur verbatim");
    await shot(page, "owner-flow-misquote");
    await cite.getByTestId("cite-value").fill("0.0042");
    await cite.getByTestId("cite-decimal").fill("0.0042");
    await cite.getByTestId("cite-submit").click();
    await expect(cite.getByTestId("cite-done")).toBeVisible();
    view = await owner.get<View>(`/matrix/${matrixId}`);
    const cited = cellOf(dragRow(), "drag-coefficient")!;
    expect(cited).toMatchObject({ value_text: "0.0042", state: "proposed" });
    const page2 = await owner.get<{ text: string }>(`/library/works/${drag.work_id}/read?page=2&max_chars=8000`);
    const at = page2.text.indexOf(SENTENCE);
    expect(cited.evidence[0]!.where).toMatchObject({ page: 2, start: at, end: at + SENTENCE.length });

    // 9. Review the cited cell; 10. export the brief.
    await page.goto(`${f.url}knowledge/matrix`);
    await page.getByTestId(`matrix-open-${matrixId}`).click();
    await page.getByTestId(`matrix-review-${drag.work_id}-drag-coefficient`).click();
    await expect(page.getByTestId(`matrix-cell-${drag.work_id}-drag-coefficient`)).toHaveAttribute("data-state", "reviewed");
    view = await owner.get<View>(`/matrix/${matrixId}`);
    expect(cellOf(dragRow(), "drag-coefficient")!.state).toBe("reviewed");

    // 11. An agent records an assumption, a finding relying on it and a correction; the owner
    // applies the correction in the hub; the row and the brief are flagged.
    const seq = async () => (await owner.get<{ project_seq: number }>("/project")).project_seq;
    const assumption = (title: string, text: string) => ({
      title, text, category: "assumption", evidence: [], claim_refs: [], author_ref: null, origin: "agent",
      quantities: [], review: "unreviewed",
    });
    const createOp = (kind: string, id: string, body: unknown) => ({ op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body });
    const relied = `entry_${crypto.randomUUID()}`;
    const replacement = `entry_${crypto.randomUUID()}`;
    const correction = `correction_${crypto.randomUUID()}`;
    await agentPost(agent, "/transactions", {
      schema_version: 1, project_id: f.project_id, request_id: crypto.randomUUID(), expected_seq: await seq(),
      operations: [createOp("entry", relied, assumption("Windage scales with p^0.8", "Assume p to 0.8."))],
    });
    const lossAt = page2.text.indexOf(LOSSES);
    const loss = await owner.get<{ evidence: unknown }>(`/library/works/${drag.work_id}/read?page=2&start=${lossAt}&end=${lossAt + LOSSES.length}`);
    await agentPost(agent, `/matrix/${matrixId}/propose`, {
      work_id: drag.work_id, column_id: "key-finding", value_text: LOSSES, evidence: [loss.evidence], relies_on: [relied],
    });
    await page.getByTestId("matrix-export-format").selectOption("markdown");
    await page.getByTestId("matrix-export-run").click();
    await expect(page.getByTestId("matrix-export-brief")).toBeVisible();
    expect((await owner.get<View>(`/matrix/${matrixId}`)).briefs).toHaveLength(1);
    await agentPost(agent, "/transactions", {
      schema_version: 1, project_id: f.project_id, request_id: crypto.randomUUID(), expected_seq: await seq(),
      operations: [
        createOp("entry", replacement, assumption("Windage scales with p^0.9", "Assume p to 0.9.")),
        createOp("correction", correction, {
          target_refs: [{ id: relied, rev: 1 }], replacement_refs: [{ id: replacement, rev: 1 }],
          reason: "the loss model was refitted", evidence: [],
          effects: [{ target_ref: { id: relied, rev: 1 }, effect: "supersede", replacement_ref: { id: replacement, rev: 1 }, replacement_text: null }],
          status: "proposed", applied_seq: null,
        }),
      ],
    });
    await page.goto(`${f.url}corrections/${correction}`);
    const shown = page.getByTestId("correction");
    await expect(shown).toHaveAttribute("data-status", "proposed");
    await page.getByRole("button", { name: "Preview applying this correction" }).click();
    // The affected list names the replacement entry by its title, not its ID (review F3).
    const affected = page.getByTestId("affected-records");
    await expect(affected).toContainText("Record “Windage scales with p^0.8” will be marked Superseded");
    await expect(affected).toContainText("replaced by “Windage scales with p^0.9”");
    await expect(affected).not.toContainText(replacement);
    await page.getByTestId("decision-preview").getByRole("button", { name: "Approve correction" }).click();
    await expect(shown).toHaveAttribute("data-status", "applied");
    view = await owner.get<View>(`/matrix/${matrixId}`);
    expect(dragRow().needs_review).toBe(true);
    expect(view.briefs.every((b) => b.needs_review)).toBe(true);
    await page.goto(`${f.url}knowledge/matrix`);
    await page.getByTestId(`matrix-open-${matrixId}`).click();
    await expect(page.getByTestId(`matrix-needs-review-${drag.work_id}`)).toBeVisible();
    await shot(page, "owner-flow-needs-review");

    // 12. Re-review the cell that relied on the superseded assumption: the row's flag clears.
    await page.getByTestId(`matrix-review-${drag.work_id}-key-finding`).click();
    await expect(page.getByTestId(`matrix-row-${drag.work_id}`)).toHaveAttribute("data-needs-review", "no");
    view = await owner.get<View>(`/matrix/${matrixId}`);
    expect(dragRow().needs_review).toBe(false);
    expect(cellOf(dragRow(), "key-finding")).toMatchObject({ value_text: LOSSES, state: "reviewed" });
    await shot(page, "owner-flow-done");
  } finally {
    await owner.ctx.dispose();
    await agent.dispose();
  }
});
