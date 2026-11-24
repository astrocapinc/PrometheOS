// Required interaction paths 2, 1 and 3 against the real hub, in that order (adoption
// produces the "next adopted action" that path 1 returns to). Every UI action is followed
// by a re-read of the records through the API.
import { expect, nav, shot, test, type Rec } from "./fixtures";

test.describe.configure({ mode: "serial" });

const COMMITMENT = "Attach the constrained-plant result before the next meeting.";

test("path 2: review a meeting, adopt the commitment (one action), leave the question unowned", async ({
  paired: page,
  hub,
  api,
}) => {
  const reconcile = await api.get(`/meetings/${hub.meeting_id}/reconcile`);
  const commitmentId: string = reconcile.items.find((i: any) => i.type === "commitment").item_id;
  const questionId: string = reconcile.items.find((i: any) => i.type === "question").item_id;
  expect(await api.records("action")).toHaveLength(0);

  await page.goto(`${hub.url}now`);
  await page.getByTestId("awaiting").getByRole("link", { name: "Review in meeting" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Supervision meeting" })).toBeVisible();
  await expect(page.getByTestId("meeting-item")).toHaveCount(3);

  const commitment = page.locator(`[data-item-id="${commitmentId}"]`);
  const question = page.locator(`[data-item-id="${questionId}"]`);
  // Proposed items beside their evidence lines.
  await commitment.getByRole("button", { name: /Show evidence in the notes/ }).click();
  const cited = page.getByTestId("meeting-notes").locator("li.cited");
  await expect(cited).toHaveCount(1);
  await expect(cited).toHaveAttribute("data-line", "3");
  await expect(cited).toContainText("I will attach the constrained-plant result");
  await question.getByRole("button", { name: /Show evidence in the notes/ }).click();
  await expect(cited).toHaveAttribute("data-line", "4");
  await expect(question.getByRole("button", { name: "Adopt as action" })).toHaveCount(0);
  await expect(question).toContainText("Owner: not agreed");

  // Decision flow: preview shows protected effects; nothing is written before Approve.
  await commitment.getByRole("button", { name: "Adopt as action" }).click();
  const preview = commitment.getByTestId("decision-preview");
  await expect(preview.getByTestId("protected-effects")).toContainText("item proposed->adopted");
  await expect(preview.getByTestId("protected-effects")).toContainText("action proposed->ready");
  await shot(page, "meeting-adopt-preview");
  expect(await api.records("action")).toHaveLength(0);
  expect((await api.record(commitmentId)).body.status).toBe("proposed");

  await preview.getByRole("button", { name: "Approve and create action" }).click();
  await expect(commitment.getByTestId("adopted-notice")).toBeVisible();
  await expect(commitment.locator('[data-state="adopted"]')).toHaveText(/Adopted/);

  // Real state: exactly one action, linked to the commitment, ready; item adopted.
  const actions = await api.records("action");
  expect(actions).toHaveLength(1);
  const action = actions[0] as Rec;
  expect(action.body.commitment_item_id).toBe(commitmentId);
  expect(action.body.status).toBe("ready");
  expect(action.body.source_refs).toEqual([{ id: commitmentId, rev: 1 }]);
  const item = await api.record(commitmentId);
  expect(item.body.status).toBe("adopted");
  expect(item.body.action_id).toBe(action.id);
  // The question stays proposed and unowned.
  const q = await api.record(questionId);
  expect(q.body.status).toBe("proposed");
  expect(q.body.proposed_owner).toBeNull();
  expect(q.body.action_id).toBeNull();
  await shot(page, "meeting-adopted");

  // The new action appears on Now.
  await nav(page, "Now").click();
  await expect(page.getByTestId("next-actions").getByTestId("action-card")).toHaveCount(1);
  await expect(page.getByTestId("next-actions")).toContainText(COMMITMENT);
});

test("path 1: return to project shows objective, latest change, next adopted action and why/evidence", async ({
  paired: page,
  hub,
  api,
}) => {
  const project = await api.get("/project");
  await page.goto(hub.url);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  await expect(page.getByTestId("objective")).toHaveText(project.settings.current_objective);

  // Latest meaningful change: the adoption just committed (action or item, same commit).
  const latest = page.getByTestId("changes").locator("li.latest");
  await expect(latest).toContainText("Latest:");
  await expect(latest).toContainText(COMMITMENT);

  const card = page.getByTestId("next-actions").getByTestId("action-card").first();
  await expect(card).toContainText(COMMITMENT);
  await expect(card).toContainText("Ready");
  await card.getByText("Why this action and its evidence").click();
  await expect(card).toContainText("From meeting commitment");
  const sourceButton = card.getByRole("button", { name: /Open source Meeting notes: Supervision meeting, line 3/ });
  await sourceButton.click();
  const panel = page.getByTestId("context-panel");
  await expect(panel.getByRole("heading", { name: "Source: Meeting notes: Supervision meeting" })).toBeFocused();
  await expect(panel.locator("li.cited")).toHaveCount(1);
  await expect(panel.locator("li.cited")).toHaveAttribute("data-line", "3");
  await expect(panel.locator("li.cited")).toContainText("I will attach the constrained-plant result");
  // The list behind stays in place: Now heading still present alongside the panel.
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  await shot(page, "now-return-with-source");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(sourceButton).toBeFocused();

  // Marking changes as seen stores only a sequence number (non-credential UI state).
  await page.getByRole("button", { name: "Mark all as seen" }).click();
  await expect(page.getByText("No changes since you last marked this project as seen.")).toBeVisible();
  const stored = await page.evaluate(() => ({ ...window.localStorage }));
  expect(Object.values(stored)).toEqual([String(await api.seq())]);
});

test("path 3: resolve a correction: compare, approve, see the claim superseded and next context reflect it", async ({
  paired: page,
  hub,
  api,
}) => {
  const old = await api.record(hub.overbroad_claim);
  const narrow = await api.record(hub.narrow_claim);
  expect(old.body.state).toBe("proposed");

  await page.goto(`${hub.url}knowledge`);
  const proposed = page.getByTestId("correction-card").filter({ hasText: "Proposed correction" });
  await expect(proposed).toHaveAttribute("data-status", "proposed");
  await proposed.getByRole("link", { name: "Review and decide" }).click();

  const correction = page.getByTestId("correction");
  await expect(correction).toHaveAttribute("data-status", "proposed");
  await expect(correction.locator(".status-banner")).toContainText("Proposed correction");
  await expect(page.getByTestId("old-claim")).toContainText(old.body.statement);
  await expect(page.getByTestId("replacement-claim")).toContainText(narrow.body.statement);
  await expect(page.getByTestId("evidence-excerpt")).toContainText("Fixture identity: PLANT-B");
  await shot(page, "correction-proposed");

  await page.getByRole("button", { name: "Preview applying this correction" }).click();
  const preview = page.getByTestId("decision-preview");
  await expect(preview.getByTestId("affected-records")).toContainText("marked Superseded");
  await expect(preview.getByTestId("affected-records")).toContainText(old.body.statement);
  await expect(preview.getByTestId("protected-effects")).toContainText("apply correction");
  // Preview wrote nothing.
  expect((await api.record(hub.overbroad_claim)).body.state).toBe("proposed");
  expect((await api.record(hub.correction_id)).body.status).toBe("proposed");

  await preview.getByRole("button", { name: "Approve correction" }).click();
  await expect(correction).toHaveAttribute("data-status", "applied");
  await expect(correction.locator(".status-banner")).toContainText("Applied correction");
  await expect(page.getByTestId("old-claim")).toContainText("Superseded");
  await expect(page.getByTestId("next-context-summary")).toContainText("lists this correction as applied");
  await expect(page.getByTestId("next-context")).toContainText("APPLIED CORRECTION");
  await shot(page, "correction-applied");

  // Real state.
  const after = await api.record(hub.overbroad_claim);
  expect(after.body.state).toBe("superseded");
  expect(after.body.superseded_by.id).toBe(hub.narrow_claim);
  expect(after.rev).toBe(old.rev + 1);
  const applied = await api.record(hub.correction_id);
  expect(applied.body.status).toBe("applied");
  expect(applied.body.applied_seq).toBeGreaterThan(0);
  const context = await api.get("/context?purpose=orient&budget=12000");
  expect(context.text).toContain("APPLIED CORRECTION");
  expect(context.text).toContain("HISTORICAL (superseded, do not rely on it)");

  // Knowledge reflects it: the overbroad claim card is superseded; corrections list applied.
  await nav(page, "Knowledge").click();
  await expect(page.locator(`[id="${hub.overbroad_claim}"]`)).toHaveAttribute("data-claim-state", "superseded");
  await expect(page.getByTestId("correction-card").first()).toHaveAttribute("data-status", "applied");
  await expect(page.getByTestId("correction-card").first()).toContainText("Applied correction");
});
