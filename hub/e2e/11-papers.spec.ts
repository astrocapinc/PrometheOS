// CP7 papers in the real hub: readiness findings from the pure check, venue requirement rows
// with their exact source lines, recording a check, a correction reaching the manuscript
// anchor and the owner approving its rebinding, file previews and the recurring-check inbox.
// Every UI action is verified by re-reading the record through a separately paired client.
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, nav, shot, test, type Api } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function serious(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

async function ensureCorrectionApplied(api: Api): Promise<void> {
  const correction = await api.record(api.hub.correction_id);
  if (correction.body.status !== "proposed") return;
  const request = {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations: [{ op: "correction_apply", id: correction.id, base_rev: correction.rev }],
  };
  const preview = await api.post("/decisions/preview", { request });
  await api.post("/decisions/apply", { request, digest: preview.digest });
}

test("paper readiness: findings, venue rows with exact lines, recording a check", async ({
  paired: page,
  hub,
  api,
}) => {
  const paperId = hub.seed.paper_id;
  await page.goto(`${hub.url}papers`);
  const card = page.getByTestId("paper-card").filter({ hasText: "Synthetic KERS manuscript" });
  await expect(card.getByTestId("paper-findings")).toContainText("readiness finding");
  await card.getByRole("link", { name: "Synthetic KERS manuscript" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic KERS manuscript" })).toBeVisible();

  const findings = page.getByTestId("findings");
  await expect(findings).toContainText("Venue requirement r1 is not yet assessed");
  await expect(findings).toContainText("Reviewer comment supervision/c1 is open");
  await expect(page.getByTestId("requirement-row")).toHaveCount(3);
  await expect(page.getByTestId("requirement-row").first()).toContainText("Not checked");

  // The requirement's exact source line opens beside the table.
  await page.getByTestId("requirement-row").first().getByRole("button", { name: /Open source/ }).click();
  const panel = page.getByTestId("context-panel");
  await expect(panel.locator("li.cited")).toHaveCount(1);
  await expect(panel.locator("li.cited")).toContainText("eight pages");
  await page.keyboard.press("Escape");

  // "Does not apply" for r3: an ordinary revision of the paper.
  const seq = await api.seq();
  const r3 = page.getByTestId("requirement-row").nth(2);
  await r3.getByLabel("Status of r3").selectOption("not_applicable");
  await r3.getByRole("button", { name: "Record check" }).click();
  await expect(page.getByTestId("requirement-row").nth(2)).toHaveAttribute("data-status", "not_applicable");
  await expect(page.getByTestId("requirement-row").nth(2)).toContainText("Does not apply");
  const paper = await api.record(paperId);
  const row = paper.body.requirements.find((r: { id: string }) => r.id === "r3");
  expect(row.applicability).toBe("not_applicable");
  expect(row.status).toBe("not_applicable");
  expect(row.last_checked_at).toMatch(/Z$/);
  expect(await api.seq()).toBe(seq + 1);

  // "Met" needs evidence: refused without a line, recorded with one.
  const r2 = page.getByTestId("requirement-row").nth(1);
  await r2.getByLabel("Status of r2").selectOption("met");
  await r2.getByRole("button", { name: "Record check" }).click();
  await expect(r2.getByRole("alert")).toContainText("Met needs evidence");
  expect(await api.seq()).toBe(seq + 1);
  await r2.getByLabel("Manuscript line showing it is met").fill("1");
  await r2.getByRole("button", { name: "Record check" }).click();
  await expect(page.getByTestId("requirement-row").nth(1)).toHaveAttribute("data-status", "met");
  const after = await api.record(paperId);
  const met = after.body.requirements.find((r: { id: string }) => r.id === "r2");
  expect(met.evidence[0].version_id).toBe(hub.seed.manuscript_version_id);
  const check = await api.get(`/papers/${paperId}/check`);
  const unchecked = check.findings.filter((f: { code: string }) => f.code === "requirement_unchecked");
  expect(unchecked.map((f: { detail: { requirement: string } }) => f.detail.requirement)).toEqual(["r1"]);
  await expect(findings).not.toContainText("Venue requirement r2");
  await shot(page, "paper-readiness");
});

test("a correction reaches the manuscript anchor; the owner approves rebinding it", async ({
  paired: page,
  hub,
  api,
}) => {
  await ensureCorrectionApplied(api);
  const anchorId = hub.seed.anchor_id;
  const flagged = await api.record(anchorId);
  expect(flagged.body.status).toBe("needs_review");
  expect(flagged.body.reason).toContain(hub.correction_id);

  await page.goto(`${hub.url}papers/${hub.seed.paper_id}`);
  const anchor = page.getByTestId("anchor-card").first();
  await expect(anchor).toHaveAttribute("data-status", "needs_review");
  await expect(anchor.getByTestId("anchor-reason")).toContainText("applied");
  const findings = page.getByTestId("findings");
  await expect(findings).toContainText("still used by a manuscript anchor");
  // Server sentences are shown in plain words: the replacement is quoted, no raw record IDs.
  await expect(findings).toContainText("In the simplified constant-deceleration event only");
  for (const text of [await findings.innerText(), await anchor.innerText()]) {
    expect(text).not.toMatch(/[a-z]+_[0-9a-f]{8}-[0-9a-f]{4}-/);
  }
  // With a replacement available, the review offers it by default.
  await expect(anchor.getByLabel("Use the replacement claim")).toBeChecked();

  // The owner reviews: rebind to the replacement claim, with a recorded reason.
  const seq = await api.seq();
  await anchor.getByLabel("Use the replacement claim").check();
  await anchor.getByLabel("Reason (recorded)").fill("The sentence now states the simplified result only.");
  await anchor.getByRole("button", { name: "Preview this review decision" }).click();
  await expect(anchor.getByText("anchor review cleared")).toBeVisible();
  await anchor.getByRole("button", { name: "Approve anchor review" }).click();
  await expect(page.getByTestId("anchor-card").first()).toHaveAttribute("data-status", "current");

  const cleared = await api.record(anchorId);
  expect(cleared.body.status).toBe("current");
  expect(cleared.body.claim_refs.map((r: { id: string }) => r.id)).toEqual([hub.narrow_claim]);
  const history = await api.get(`/records/${anchorId}/history`);
  const last = history.transitions[history.transitions.length - 1];
  expect(last.reason).toBe("The sentence now states the simplified result only.");
  expect(last.protected_effects).toEqual(["anchor review cleared"]);
  expect(await api.seq()).toBe(seq + 1); // rebinding and clearing commit together
  await shot(page, "paper-anchor-rebound");
});

test("previews, files used by the paper and the recurring-check inbox", async ({
  paired: page,
  hub,
  api,
}) => {
  await page.goto(`${hub.url}papers/${hub.seed.paper_id}`);
  await page.getByRole("button", { name: "Open source manuscript.md, whole source" }).click();
  await expect(page.getByTestId("context-panel")).toContainText("Synthetic manuscript fragment");
  await page.keyboard.press("Escape");

  const pdf = page.getByTestId("used-files").getByTestId("open-file");
  await expect(pdf).toHaveAttribute("target", "_blank");
  const href = await pdf.getAttribute("href");
  expect(href).toBe(`/api/v1/versions/${hub.seed.pdf_version_id}/content`);
  const reply = await page.request.get(`${hub.url.replace(/\/$/, "")}${href}`);
  expect(reply.status()).toBe(200);
  expect(reply.headers()["content-type"]).toBe("application/pdf");
  expect(reply.headers()["content-security-policy"]).not.toContain("script-src");
  expect((await reply.body()).subarray(0, 5).toString()).toBe("%PDF-");
  expect(await serious(page)).toEqual([]);

  await nav(page, "Papers").click();
  const inbox = page.getByTestId("inbox");
  await expect(inbox.getByTestId("inbox-item")).toHaveCount(2);
  await expect(inbox).toContainText("Paper readiness changed");
  await expect(inbox).toContainText("Project digest");
  const seq = await api.seq();
  await inbox.getByRole("button", { name: /Dismiss: Project digest/ }).click();
  await expect(inbox.getByTestId("inbox-item")).toHaveCount(1);
  expect((await api.get("/inbox")).items).toHaveLength(1);
  await page.getByRole("button", { name: "Run due checks now" }).click();
  await expect(page.getByTestId("tick-result")).toContainText("No recurring check was due.");
  expect(await api.seq()).toBe(seq); // dismissal and a quiet tick record nothing
  expect(await serious(page)).toEqual([]);
  await shot(page, "papers-inbox");
});
