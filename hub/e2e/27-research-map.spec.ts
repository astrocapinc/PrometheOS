// CP20.2 (A06.1; C20-STRUCTURE, S62; decision 0083) in the real hub: a paper with two questions,
// claims answering them (one supported only by an agent's assessment) and a track they draw on.
// The paper page shows each question with its claims, one-line status and basis, the derived
// stage with what keeps it from the next and the agent-reported claim to confirm; no identifier,
// digest or path shows outside a claim's details. Now leads with the deliverables.
import AxeBuilder from "@axe-core/playwright";
import { expect, shot, test, type Api } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
const SCOPE = {
  system: "Synthetic plant",
  operating_regime: "Urban cycle",
  comparator: "Same vehicle without the device",
  model_fidelity: "Reduced model",
  validation_level: "Re-read from the stored artefact",
};

function create(kind: string, id: string, body: unknown) {
  return { op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body };
}

function entry(title: string, category: string) {
  return {
    title,
    text: `${title}.`,
    category,
    evidence: [],
    claim_refs: [],
    author_ref: null,
    origin: "agent",
    quantities: [],
    review: "unreviewed",
  };
}

function rel(source: string, target: string, relation: string) {
  return { op: "relation_add", source_ref: { id: source, rev: 1 }, target_ref: { id: target, rev: 1 }, relation };
}

async function transact(api: Api, operations: unknown[]): Promise<void> {
  await api.post("/transactions", {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations,
  });
}

async function seedMap(api: Api) {
  const version = await api.record(api.hub.seed.manuscript_version_id);
  const ev = {
    source_id: version.body.source_id,
    version_id: version.id,
    locator: { type: "text", start_line: 1, end_line: 1 },
    quote_sha256: null,
  };
  const id = (kind: string) => `${kind}_${crypto.randomUUID()}`;
  const ids = {
    paper: id("paper"),
    q1: id("entry"),
    q2: id("entry"),
    track: id("entry"),
    held: id("claim"),
    open: id("claim"),
    withdrawn: id("paper"),
  };
  const claim = (statement: string, summary: string) => ({
    statement,
    scope: SCOPE,
    evidence: [ev],
    assessment_refs: [],
    state: "proposed",
    superseded_by: null,
    summary,
  });
  await transact(api, [
    create("entry", ids.q1, entry("Does the sun lock cost energy on heavy duties?", "question")),
    create("entry", ids.q2, entry("Can a partial hold replace the predictor?", "question")),
    create("entry", ids.track, entry("Track 12: full-cycle plant", "track")),
    create("claim", ids.held, claim("The lock-aware controller still costs energy against the EV on every heavy duty cycle.", "The lock costs energy on every heavy duty")),
    create("claim", ids.open, claim("A constant partial hold recovers most of the oracle benefit with no predictor.", "A partial hold recovers most of the benefit")),
    create("paper", ids.paper, {
      title: "Research map paper (synthetic)",
      objective: "Show the questions with their claims.",
      claim_refs: [
        { id: ids.held, rev: 1 },
        { id: ids.open, rev: 1 },
      ],
      manuscript_source_id: null,
      venue_source_refs: [],
      status: "drafting",
      deadline: null,
      requirements: [],
      contribution: "A synthetic map.",
      related_papers: [],
      review_rounds: [],
    }),
    create("paper", ids.withdrawn, {
      title: "Withdrawn map paper (synthetic)",
      objective: "A paper the owner withdrew.",
      claim_refs: [],
      manuscript_source_id: null,
      venue_source_refs: [],
      status: "planning",
      deadline: null,
      requirements: [],
      contribution: "None.",
      related_papers: [],
      review_rounds: [],
    }),
  ]);
  await transact(api, [
    { op: "transition", id: ids.withdrawn, base_rev: 1, to: "withdrawn", reason: "Withdrawn by the owner.", evidence: [] },
    create("assessment", id("assessment"), {
      claim_ref: { id: ids.held, rev: 1 },
      outcome: "supports",
      rationale: "Re-read from the stored artefact.",
      checks: [],
      author_ref: null,
      method: "reported_agent",
      scope_match: true,
    }),
    rel(ids.held, ids.q1, "answers"),
    rel(ids.open, ids.q2, "answers"),
    rel(ids.q1, ids.paper, "addresses"),
    rel(ids.q2, ids.paper, "addresses"),
    rel(ids.held, ids.track, "draws_on"),
  ]);
  return { ...ids, claim, ev };
}

/** The owner supersedes a claim through the decision preview and apply, as in 11-papers. */
async function supersedeByOwner(api: Api, oldId: string, newId: string): Promise<void> {
  const corr = `correction_${crypto.randomUUID()}`;
  await transact(api, [
    create("correction", corr, {
      target_refs: [{ id: oldId, rev: 1 }],
      replacement_refs: [{ id: newId, rev: 1 }],
      reason: "The published climb replaces the stored cycle.",
      evidence: [],
      effects: [
        {
          target_ref: { id: oldId, rev: 1 },
          effect: "supersede",
          replacement_ref: { id: newId, rev: 1 },
          replacement_text: null,
        },
      ],
      status: "proposed",
      applied_seq: null,
    }),
  ]);
  const request = {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations: [{ op: "correction_apply", id: corr, base_rev: 1 }],
  };
  const preview = await api.post("/decisions/preview", { request });
  await api.post("/decisions/apply", { request, digest: preview.digest });
}

test("the paper page shows questions with their claims and the derived stage (S62)", async ({
  paired: page,
  hub,
  api,
}) => {
  const ids = await seedMap(api);
  await page.goto(`${hub.url}papers/${ids.paper}`);
  const map = page.getByTestId("paper-map");
  await expect(map.getByTestId("map-question")).toHaveCount(2);
  const first = map.getByTestId("map-question").filter({ hasText: "sun lock" });
  await expect(first.getByTestId("map-claim")).toContainText("The lock costs energy on every heavy duty");
  await expect(first.getByTestId("claim-status")).toHaveAttribute("data-status", "holds");
  await expect(first.getByTestId("claim-status")).toContainText("your agent's check, not yet reviewed");
  const second = map.getByTestId("map-question").filter({ hasText: "partial hold" });
  await expect(second.getByTestId("claim-status")).toHaveAttribute("data-status", "unverified");

  // Derived stage: framed and evidence mapped, verified next, with what keeps it back.
  const steps = map.getByTestId("stage-bar").locator("li");
  await expect(steps.nth(0)).toHaveAttribute("data-reached", "yes");
  await expect(steps.nth(1)).toHaveAttribute("data-reached", "yes");
  await expect(steps.nth(2)).toHaveAttribute("data-reached", "no");
  await expect(map.getByTestId("stage-next")).toContainText("A partial hold recovers most of the benefit");
  await expect(map.getByTestId("deliverable-headline")).toHaveText(
    "1 of 2 claims under 2 questions still to verify.",
  );
  await expect(map.getByTestId("to-confirm")).toContainText("The lock costs energy on every heavy duty");
  await expect(map.getByTestId("map-note")).toContainText("descriptive");

  // Readiness does not repeat the claims listed above it.
  await expect(page.getByTestId("findings-folded")).toBeVisible();
  await expect(page.locator("[data-testid=finding]", { hasText: "A partial hold" })).toHaveCount(0);

  // No identifier, digest or path outside a claim's details (closed details render no text).
  const visible = await page.locator("main").innerText();
  expect(visible).not.toMatch(/\b(claim|entry|paper|version|source|assessment)_[0-9a-f-]{8,}/);
  expect(visible).not.toMatch(/\b[0-9a-f]{12,}\b/);
  expect(await map.innerText()).not.toContain("manuscript.md");

  // The details hold them.
  await first.getByText("Details").click();
  const details = first.getByTestId("claim-details");
  await expect(details).toContainText(ids.held);
  await expect(details).toContainText("sha256");
  await expect(details).toContainText("manuscript.md");
  await shot(page, "27-paper-map");

  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => v.id)).toEqual([]);

  // Now leads with the deliverables and their stages.
  await page.goto(hub.url);
  const card = page.getByTestId("deliverable").filter({ hasText: "Research map paper (synthetic)" });
  await expect(card.getByTestId("deliverable-headline")).toHaveText(
    "1 of 2 claims under 2 questions still to verify.",
  );
  await expect(card.getByTestId("stage-bar").locator("li")).toHaveCount(6);
  await expect(card.getByTestId("claim-health")).toHaveText("Claims: 1 hold, 1 unverified");
  await expect(card.getByTestId("stage-summary")).toHaveText(
    "Evidence mapped · next: Verified · paper status: Drafting",
  );
  // A withdrawn paper is not a deliverable on the landing.
  await expect(page.getByTestId("deliverable").filter({ hasText: "Withdrawn map paper" })).toHaveCount(0);
  const order = await page.locator("main h2").allInnerTexts();
  expect(order[0]).toBe("Deliverables");
  await shot(page, "27-now-deliverables");

  // The Papers list shows each paper's stage.
  await page.goto(`${hub.url}papers`);
  const listed = page.getByTestId("paper-card").filter({ hasText: "Research map paper (synthetic)" });
  await expect(listed.getByTestId("paper-stage")).toHaveText(
    "Stage: Evidence mapped · next: Verified · paper status: Drafting",
  );

  // The map read changed nothing: the claim is still proposed (never promoted).
  expect((await api.record(ids.held)).body.state).toBe("proposed");

  // The owner supersedes the open claim: its replacement answers the same question.
  const replacement = `claim_${crypto.randomUUID()}`;
  await transact(api, [
    create("claim", replacement, ids.claim("A partial hold at f = 0.3 recovers 67 % of the oracle benefit.", "A partial hold recovers two thirds of the benefit")),
  ]);
  await supersedeByOwner(api, ids.open, replacement);
  await page.goto(`${hub.url}papers/${ids.paper}`);
  const answered = page.getByTestId("paper-map").getByTestId("map-question").filter({ hasText: "partial hold" });
  const row = answered.getByTestId("map-claim").filter({ hasText: "two thirds" });
  await expect(row).toContainText("Replaces a superseded claim under this question.");
  await expect(answered.getByTestId("map-claim").filter({ hasText: "recovers most" })).toHaveAttribute(
    "data-status",
    "superseded",
  );

  // A failed map read shows an error instead of hiding the section.
  await page.route("**/api/v1/knowledge/research-map**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: { code: "internal_error", message: "synthetic failure" } }),
    }),
  );
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Research map paper (synthetic)" })).toBeVisible();
  await expect(page.locator(".state-error").first()).toBeVisible();
  await expect(page.getByTestId("paper-map")).toHaveCount(0);
});
