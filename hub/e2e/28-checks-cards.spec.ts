// CP20.3 (A06.1; C20-VERIFY, S63, S64; decision 0084) in the real hub: a decision card on Now
// with what, why, recommendation and default; "Apply all recommended" as one confirmation that
// applies the card's plan; planned checks from PrometheOS's detector with the budget, and which
// ones the agent may start. Every outcome is read back through the API.
import AxeBuilder from "@axe-core/playwright";
import { expect, shot, test, type Api } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
const SCOPE = {
  system: "Synthetic plant",
  operating_regime: "Mine haul",
  comparator: "Same vehicle without the device",
  model_fidelity: "Reduced model",
  validation_level: "Re-read from the stored artefact",
};

function create(kind: string, id: string, body: unknown) {
  return { op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body };
}

async function transact(api: Api, operations: unknown[], extra: Record<string, unknown> = {}, status = 200) {
  return api.post(
    "/transactions",
    {
      schema_version: 1,
      project_id: api.hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await api.seq(),
      operations,
      ...extra,
    },
    status,
  );
}

test("a decision card, apply all recommended, and planned checks on Now (S63, S64)", async ({
  paired: page,
  hub,
  api,
}) => {
  const version = await api.record(hub.seed.manuscript_version_id);
  const ev = {
    source_id: version.body.source_id,
    version_id: version.id,
    locator: { type: "text", start_line: 1, end_line: 1 },
    quote_sha256: null,
  };
  const claimId = `claim_${crypto.randomUUID()}`;
  const corrId = `correction_${crypto.randomUUID()}`;
  await transact(api, [
    create("claim", claimId, {
      statement: "On a mine haul the rotor's energy is negligible against about 200 MJ of uphill demand.",
      scope: SCOPE,
      evidence: [ev],
      assessment_refs: [],
      state: "proposed",
      superseded_by: null,
      summary: "The mine-haul rotor energy is negligible",
    }),
  ]);
  await transact(api, [
    create("correction", corrId, {
      target_refs: [{ id: claimId, rev: 1 }],
      replacement_refs: [],
      reason: "The stored cycle applied the steepest grade everywhere.",
      evidence: [],
      effects: [
        { target_ref: { id: claimId, rev: 1 }, effect: "withdraw", replacement_ref: null, replacement_text: null },
      ],
      status: "proposed",
      applied_seq: null,
    }),
  ]);
  // The request needs the owner, so it waits as an approval request carrying its card.
  const asked = await transact(
    api,
    [{ op: "correction_apply", id: corrId, base_rev: 1 }],
    {
      card: {
        what: "Mine haul (synthetic): withdraw the ~200 MJ claim",
        why: "The stored cycle applied the steepest grade everywhere; the published climb is 63.6 MJ.",
        recommendation: "Withdraw it and quote the corrected comparison instead.",
        default_text: "Views show the claim as settling until you decide.",
        default_at: "2026-11-05T09:00:00Z",
      },
    },
    403,
  );
  expect(asked.error.code).toBe("approval_required");

  await page.goto(hub.url);
  const cards = page.getByTestId("decision-cards");
  const card = cards.getByTestId("decision-card").filter({ hasText: "Mine haul (synthetic)" });
  await expect(card).toContainText("Recommended: Withdraw it and quote the corrected comparison instead.");
  await expect(card.getByTestId("card-default")).toContainText("Views show the claim as settling");
  await expect(card).toContainText("Only views change");
  // The claim reads as settling while the card waits.
  const status = await api.get<{ claims: { status: string; causes: { code: string }[] }[] }>(
    `/knowledge/claim-status?claim_id=${claimId}`,
  );
  expect(status.claims[0].status).toBe("settling");
  expect(status.claims[0].causes.map((c) => c.code)).toContain("card_waiting");

  // Planned checks: PrometheOS proposes them; the budget is shown with what the agent may start.
  const checks = page.getByTestId("planned-checks");
  await expect(checks.getByTestId("budget")).toContainText("up to 15 min each and 60 min a day");
  await checks.getByTestId("plan-checks").click();
  await expect(checks.getByRole("status")).toContainText("new check(s) planned");
  await expect(checks.getByTestId("planned-check").first()).toBeVisible();
  const plan = await api.get<{ checks: { claim_id: string; eligible: boolean; kind: string }[] }>(
    "/verification/plan",
  );
  const mine = plan.checks.filter((c) => c.claim_id === claimId);
  expect(mine.length).toBeGreaterThan(0);
  expect(mine.every((c) => c.kind === "read_only" && c.eligible)).toBe(true);
  await shot(page, "28-now-cards-checks");

  const results = await new AxeBuilder({ page }).include("[data-testid=decision-cards]").withTags(TAGS).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => v.id)).toEqual([]);

  // Apply all recommended: one preview, one confirmation, one transaction.
  const before = await api.seq();
  await cards.getByTestId("apply-recommended").click();
  await expect(cards.getByTestId("recommended-preview")).toContainText("If any of them changed since, nothing is applied.");
  // What the owner approves is PrometheOS's own list of effects, not only the agent's wording.
  await expect(cards.getByTestId("recommended-effects").locator("li").first()).toBeVisible();
  await cards.getByRole("button", { name: "Confirm: apply all" }).click();
  await expect(cards.getByRole("status")).toContainText("Applied");
  await expect(card).toHaveCount(0);
  expect((await api.record(corrId)).body.status).toBe("applied");
  expect((await api.record(claimId)).body.state).toBe("withdrawn");
  expect(await api.seq()).toBe(before + 1);
});

test("one card stays answerable after unrelated writes and applies alone (decision 0107)", async ({
  paired: page,
  hub,
  api,
}) => {
  const version = await api.record(hub.seed.manuscript_version_id);
  const ev = {
    source_id: version.body.source_id,
    version_id: version.id,
    locator: { type: "text", start_line: 1, end_line: 1 },
    quote_sha256: null,
  };
  const claimId = `claim_${crypto.randomUUID()}`;
  const corrId = `correction_${crypto.randomUUID()}`;
  await transact(api, [
    create("claim", claimId, {
      statement: "Single card (synthetic): the bus saves 18.2 % fuel on the urban cycle.",
      scope: SCOPE,
      evidence: [ev],
      assessment_refs: [],
      state: "proposed",
      superseded_by: null,
    }),
    create("correction", corrId, {
      target_refs: [{ id: claimId, rev: 1 }],
      replacement_refs: [],
      reason: "The later summary corrects the figure.",
      evidence: [],
      effects: [
        { target_ref: { id: claimId, rev: 1 }, effect: "withdraw", replacement_ref: null, replacement_text: null },
      ],
      status: "proposed",
      applied_seq: null,
    }),
  ]);
  const asked = await transact(
    api,
    [{ op: "correction_apply", id: corrId, base_rev: 1 }],
    {
      card: {
        what: "Single card (synthetic): withdraw the 18.2 % claim",
        why: "The later summary corrects it.",
        recommendation: "Withdraw it.",
        default_text: "Views show the claim as settling until you decide.",
      },
    },
    403,
  );
  expect(asked.error.code).toBe("approval_required");
  // An unrelated write afterwards: the card's own records are unchanged, so it still applies.
  await transact(api, [
    create("entry", `entry_${crypto.randomUUID()}`, {
      title: "Unrelated note",
      text: "Written after the card.",
      category: "note",
      evidence: [],
      claim_refs: [],
      author_ref: null,
      origin: "agent",
      quantities: [],
      review: "unreviewed",
    }),
  ]);
  await page.goto(hub.url);
  const cards = page.getByTestId("decision-cards");
  const card = cards.getByTestId("decision-card").filter({ hasText: "Single card (synthetic)" });
  await expect(card).toContainText("Only views change");
  await card.getByTestId("apply-this-card").click();
  await expect(cards.getByTestId("recommended-preview")).toContainText("Apply this recommendation");
  await cards.getByRole("button", { name: "Confirm: apply it" }).click();
  await expect(cards.getByRole("status")).toContainText("Applied 1 recommendation");
  expect((await api.record(corrId)).body.status).toBe("applied");
  expect((await api.record(claimId)).body.state).toBe("withdrawn");
});

test("a planned check whose cause is gone says you can close it (gap audit #10, #16)", async ({
  paired: page,
  hub,
  api,
}) => {
  const version = await api.record(hub.seed.manuscript_version_id);
  const claimId = `claim_${crypto.randomUUID()}`;
  await transact(api, [
    create("claim", claimId, {
      statement: "The reduced model reproduces the stored urban-cycle energy within a tenth.",
      scope: SCOPE,
      evidence: [
        { source_id: version.body.source_id, version_id: version.id, locator: { type: "text", start_line: 1, end_line: 1 }, quote_sha256: null },
      ],
      assessment_refs: [],
      state: "proposed",
      superseded_by: null,
      summary: "The reduced model reproduces the stored energy",
    }),
  ]);
  await api.post("/verification/propose", {});
  type Row = { action_id: string; claim_id: string | null; resolved: boolean };
  const plan = async () =>
    (await api.get<{ checks: Row[] }>("/verification/plan")).checks.filter((c) => c.claim_id === claimId);
  const planned = await plan();
  expect(planned.length).toBeGreaterThan(0);
  expect(planned.some((c) => c.resolved)).toBe(false);
  // A supporting assessment removes the cause the check was planned for.
  await transact(api, [
    create("assessment", `assessment_${crypto.randomUUID()}`, {
      claim_ref: { id: claimId, rev: 1 },
      outcome: "supports",
      rationale: "Re-read from the stored artefact.",
      checks: [],
      author_ref: null,
      method: "reported_agent",
      scope_match: true,
    }),
  ]);
  const after = await plan();
  const gone = after.filter((c) => c.resolved);
  expect(gone.length).toBeGreaterThan(0);
  await page.goto(hub.url);
  const row = page.getByTestId("planned-checks").getByTestId("checks-resolved").locator(`[data-action-id="${gone[0].action_id}"]`);
  await expect(row.getByTestId("check-resolved")).toHaveText("its cause is gone; you can close it");
  await expect(row.getByRole("link", { name: "close it" })).toHaveAttribute("href", `/actions/${gone[0].action_id}`);
});

test("the owner changes the verification budget on Now after a preview (gap audit #16)", async ({
  paired: page,
  hub,
  api,
}) => {
  type Plan = { budget: { max_minutes_each: number; max_minutes_per_day: number; output_root_alias: string | null; output_path: string | null } };
  await page.goto(hub.url);
  const checks = page.getByTestId("planned-checks");
  await checks.getByText("Change the budget").click();
  const form = checks.getByTestId("budget-form");
  await form.getByLabel("Minutes per check").fill("20");
  await form.getByLabel("Minutes per day").fill("90");
  await form.getByLabel("Output folder: research folder").selectOption("research");
  await form.getByLabel("Folder inside it").fill("checks/out");
  const before = await api.seq();
  await form.getByTestId("budget-review").click();
  // What the owner approves is PrometheOS's own wording of the change; nothing is applied yet.
  await expect(form.getByTestId("budget-effects")).toContainText("verification budget change");
  await expect(form.getByTestId("budget-effects")).toContainText("20 min each, 90 min a day");
  expect(await api.seq()).toBe(before);
  await form.getByTestId("budget-apply").click();
  await expect(checks.getByTestId("budget")).toContainText("up to 20 min each and 90 min a day");
  await expect(checks.getByTestId("budget")).toContainText("New files go to research/checks/out.");
  expect(await api.seq()).toBe(before + 1);
  const plan = await api.get<Plan>("/verification/plan");
  expect(plan.budget).toMatchObject({
    max_minutes_each: 20,
    max_minutes_per_day: 90,
    output_root_alias: "research",
    output_path: "checks/out",
  });
  // The research folders are unchanged: the browser never sent their paths.
  const roots = await api.get<{ roots: { alias: string; available: boolean }[] }>("/project/roots");
  expect(roots.roots.find((r) => r.alias === "research")?.available).toBe(true);
  await shot(page, "28-budget");
});
