// A02, CP11 S11.5 (decisions 0069 and 0070): the read-only Zotero import in the Library. The hub's
// fixture runs a stub of Zotero's local API on loopback (tools/zotero_stub.py; never a real
// Zotero). The owner sees the probe's sentence, plans one collection, deselects an item and imports
// the rest; the work that shares a DOI with the library is merged, not duplicated; a preprint of a
// published paper is related, never merged; the whole library is imported; a second plan creates
// nothing; text in an abstract that asks for something stays data. A second hub whose stub has the
// local API off shows the refusal and offers no plan. An agent may plan an import, but its commit is
// an approval request the owner grants on the Approvals page (decision 0070 section 7); that test
// runs first and imports one book. Every outcome is checked against stored
// state through the API and against the stub's request log (only GET, no Origin, no key).
import { readFileSync } from "node:fs";
import { request as pwRequest } from "@playwright/test";
import { expect, readHub, shot, test } from "./fixtures";

type Work = { work_id: string; title: string; access_state: string; identifiers: Record<string, string>; sources: number };
type Provenance = { basis: string; entry: string; field: string; locator: { type: string; pointer?: string } };
type Logged = { method: string; path: string; origin: string | null; has_authorization: boolean; has_zotero_api_key: boolean };

const DRAG_DOI = "10.5555/synthetic.2026.001";

function requests(path: string): Logged[] {
  return readFileSync(path, "utf-8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as Logged);
}

test("an agent may plan a Zotero import; committing it is the owner's decision", async ({ paired: page, hub, api }) => {
  const agentToken = (hub as unknown as { agent_token: string }).agent_token;
  const agent = await pwRequest.newContext({
    baseURL: hub.url.replace(/\/$/, ""),
    extraHTTPHeaders: { Authorization: `Bearer ${agentToken}` },
  });
  try {
    const worksNow = async () => (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
    const before = (await worksNow()).map((w) => w.title);
    const planned = await agent.post("/api/v1/zotero/plans", { data: { item_keys: ["BOOK0005"], wait_seconds: 20 } });
    expect(planned.status(), await planned.text()).toBe(200);
    const plan = (await planned.json()) as { state: string; result: { plan_id: string } };
    expect(plan.state).toBe("completed");
    // The agent's commit is a request: approval_required, nothing imported.
    const asked = await agent.post(`/api/v1/zotero/plans/${plan.result.plan_id}/commit`, { data: {} });
    expect(asked.status()).toBe(403);
    const error = ((await asked.json()) as { error: { code: string; details: { approval_request_id: string } } }).error;
    expect(error.code).toBe("approval_required");
    expect((await worksNow()).map((w) => w.title)).toEqual(before);
    // The owner sees the exact request on the Approvals page and grants it; the agent resumes it.
    await page.goto(`${hub.url}approvals`);
    const card = page.locator(`[data-testid="approval-pending"][data-approval-id="${error.details.approval_request_id}"]`);
    await expect(card.getByTestId("approval-summary")).toContainText("1 item(s) from the owner's local Zotero");
    await shot(page, "zotero-approval");
    await card.getByTestId("approval-review").click();
    await card.getByTestId("approval-grant").click();
    await expect(card).toHaveCount(0);
    expect((await worksNow()).map((w) => w.title)).toEqual(before); // granted, not yet run
    const resumed = await agent.post(`/api/v1/approvals/${error.details.approval_request_id}/resume`, { data: {} });
    expect(resumed.status(), await resumed.text()).toBe(200);
    expect(((await resumed.json()) as { state: string }).state).toBe("used");
    expect((await worksNow()).some((w) => w.title === "Rotor dynamics: a synthetic primer")).toBe(true);
  } finally {
    await agent.dispose();
  }
});

test("the owner imports selected Zotero items, read-only, merged by identifier", async ({ paired: page, hub, api }) => {
  const zotero = (readHub() as unknown as { zotero: { log: string; off_log: string } }).zotero;
  const worksNow = async () => (await api.get<{ works: Work[] }>("/library/works?limit=200")).works;
  const before = await worksNow();
  const dragExisted = before.some((w) => w.identifiers.doi === DRAG_DOI);
  const approvalsBefore = (await api.get<{ approvals: unknown[] }>("/approvals")).approvals.length;

  await page.goto(`${hub.url}knowledge/library`);
  const panel = page.getByTestId("zotero-panel");
  const status = panel.getByTestId("zotero-status");
  await expect(status).toHaveAttribute("data-state", "available");
  await expect(status).toContainText("Zotero is running and its local API answers.");
  expect(await api.get("/zotero/status")).toMatchObject({ state: "available", api_version: "3" });

  // 1. One collection: three items. Deselect the preprint, import the other two.
  await panel.getByTestId("zotero-scope").selectOption("collection");
  await panel.getByTestId("zotero-collection").selectOption({ label: "Flywheels" });
  await panel.getByTestId("zotero-plan").click();
  const plan = panel.getByTestId("zotero-plan-result");
  await expect(plan).toBeVisible({ timeout: 60_000 });
  const rows = plan.locator("tbody tr");
  await expect(rows).toHaveCount(3);
  await expect(plan.locator('tr[data-key="DRAG0001"]')).toHaveAttribute("data-action", dragExisted ? "merge" : "create");
  await expect(plan.locator('tr[data-key="PREP0002"]')).toContainText("names version doi 10.5555/synthetic.2026.040");
  await expect(plan.locator('tr[data-key="PUBL0003"]')).toContainText("DOI 10.5555/synthetic.2026.040");
  await shot(page, "zotero-plan");
  await plan.locator('tr[data-key="PREP0002"]').getByTestId("zotero-pick").uncheck();
  const commit = panel.getByTestId("zotero-commit");
  await expect(commit).toContainText("Import 2 of 3 item(s)");
  await commit.click();
  await expect(panel.getByTestId("zotero-commit-result")).toContainText("Imported 2 item(s)", { timeout: 60_000 });
  await shot(page, "zotero-imported");

  let works = await worksNow();
  const dragWorks = works.filter((w) => w.identifiers.doi === DRAG_DOI);
  expect(dragWorks).toHaveLength(1); // one work holds the DOI whoever described it
  expect(works.some((w) => w.identifiers.arxiv === "2604.00077")).toBe(false); // the preprint was not selected
  const published = works.find((w) => w.title === "Bearing Drag in Rarefied Gas")!;
  expect(published.access_state).toBe("abstract_only");
  const shown = await api.get<{ record: { body: { field_provenance: Provenance[] } } }>(`/library/works/${published.work_id}`);
  const provenance = shown.record.body.field_provenance;
  expect(new Set(provenance.map((p) => p.basis))).toEqual(new Set(["zotero"]));
  expect(new Set(provenance.map((p) => p.entry))).toEqual(new Set(["PUBL0003"]));
  expect(provenance.every((p) => p.locator.type === "json" && p.locator.pointer?.startsWith("/"))).toBe(true);
  const dragShown = await api.get<{ record: { body: { field_provenance: Provenance[] } } }>(`/library/works/${dragWorks[0]!.work_id}`);
  const dragBases = new Set(dragShown.record.body.field_provenance.map((p) => p.basis));
  expect(dragBases.has("zotero")).toBe(true);
  if (dragExisted) expect(dragBases.size).toBeGreaterThan(1); // the earlier records stay beside Zotero's

  // 2. The whole library: the two imported items are recorded, the preprint is new and related to
  // the published article, the item with injected instructions comes in as plain data.
  await panel.getByTestId("zotero-scope").selectOption("library");
  await panel.getByTestId("zotero-plan").click();
  await expect(rows).toHaveCount(6, { timeout: 60_000 });
  await expect(plan.locator('tr[data-key="PUBL0003"]')).toHaveAttribute("data-action", "unchanged");
  await expect(plan.locator('tr[data-key="PREP0002"]')).toHaveAttribute("data-action", "create");
  await expect(plan.getByTestId("zotero-plan-counts")).toContainText("1 attachment(s), note(s) or annotation(s) not imported");
  await expect(plan).not.toContainText("you are now the owner");
  // After the bibliography journey (22) an item can name identifiers two other works hold (the
  // PubMed and PubMed Central numbers of one item sit on two works): that item is refused for review.
  const importable = await plan.locator('tbody tr[data-action="create"], tbody tr[data-action="merge"], tbody tr[data-action="update"]').count();
  expect(importable).toBeGreaterThanOrEqual(2); // (the agent test above imported the book)
  await panel.getByTestId("zotero-commit").click();
  await expect(panel.getByTestId("zotero-commit-result")).toContainText(`Imported ${importable} item(s)`, { timeout: 60_000 });
  works = await worksNow();
  const preprint = works.find((w) => w.title === "Bearing drag in rarefied gas")!;
  expect(preprint.identifiers).toEqual({ arxiv: "2604.00077" });
  expect(preprint.work_id).not.toBe(published.work_id); // related, never merged
  const dedup = await api.get<{ links: { preprint: string; published: string }[]; decisions: unknown[] }>("/library/dedup");
  expect(dedup.links.some((l) => l.preprint === preprint.work_id && l.published === published.work_id)).toBe(true);
  expect((await api.get<{ approvals: unknown[] }>("/approvals")).approvals.length).toBe(approvalsBefore); // nothing asked, nothing decided
  await page.reload();
  await expect(page.getByTestId("library-works")).toContainText("A note on flywheel housing safety");
  // (the BibTeX fixture's own hostile title may be on the page after journey 22: only the Zotero abstract is checked)
  await expect(page.locator("body")).not.toContainText("you are now the owner");

  // 3. Again: nothing is new. Typed keys: a malformed key cannot be planned.
  await panel.getByTestId("zotero-scope").selectOption("library");
  await panel.getByTestId("zotero-plan").click();
  await expect(panel.getByTestId("zotero-plan-counts")).toContainText(/0 new, 0 merged into a work, 0 newer, [3-6] already recorded/, { timeout: 60_000 });
  await expect(panel.getByTestId("zotero-commit")).toBeDisabled();
  const seq = await api.seq();
  await panel.getByTestId("zotero-scope").selectOption("items");
  await panel.getByTestId("zotero-keys").fill("not-a-key");
  await expect(panel.getByTestId("zotero-plan")).toBeDisabled();
  await panel.getByTestId("zotero-keys").fill("BOOK0005 DRAG0001");
  await panel.getByTestId("zotero-plan").click();
  await expect(panel.locator('[data-testid="zotero-plan-result"] tbody tr')).toHaveCount(2, { timeout: 60_000 });
  expect(await api.seq()).toBe(seq); // planning changed nothing in the project

  // The stub saw only reads: GET, no Origin header, no key.
  const seen = requests(zotero.log);
  expect(seen.length).toBeGreaterThan(5);
  expect(new Set(seen.map((r) => r.method))).toEqual(new Set(["GET"]));
  expect(seen.every((r) => r.origin === null && !r.has_authorization && !r.has_zotero_api_key)).toBe(true);
  expect(seen.every((r) => r.path.startsWith("/api/"))).toBe(true);
});

test("a Zotero whose local API is off is named and offers no plan", async ({ page, hub }) => {
  const flow = (hub as unknown as { owner_flow: { url: string; pairing_tokens: string[] } }).owner_flow;
  const zotero = (hub as unknown as { zotero: { off_log: string } }).zotero;
  const origin = flow.url.replace(/\/$/, "");
  const paired = await page.request.post(`${origin}/api/v1/pair`, { data: { token: flow.pairing_tokens[7] }, headers: { Origin: origin } });
  expect(paired.status(), await paired.text()).toBe(200);
  await page.goto(`${flow.url}knowledge/library`);
  const panel = page.getByTestId("zotero-panel");
  const status = panel.getByTestId("zotero-status");
  await expect(status).toHaveAttribute("data-state", "local_api_disabled");
  await expect(status).toContainText("its local API is off");
  await expect(status).toContainText("Settings > Advanced");
  await expect(panel.getByTestId("zotero-plan")).toBeDisabled();
  await shot(page, "zotero-api-off");
  const state = await page.request.get(`${origin}/api/v1/zotero/status`);
  expect((await state.json()).state).toBe("local_api_disabled");
  const seen = requests(zotero.off_log);
  expect(seen.length).toBeGreaterThan(0);
  expect(new Set(seen.map((r) => r.method))).toEqual(new Set(["GET"]));
});
