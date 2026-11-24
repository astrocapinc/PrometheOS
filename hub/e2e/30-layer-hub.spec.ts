// The intelligence layer in the real hub (decisions 0109, 0110; advisory F6 of review
// map-ai-spread-a). The brief opens Now in the API's own order. An unplaced claim shows the layer's
// proposal (two recorded models agree) and "Place it there" writes one `answers` link, read back
// from the store with both revisions. A rerun's result shows its draft reading (expected,
// measured, the difference; no tolerance), and a decision card shows the layer's wording with
// the bound number filled in by PrometheOS, its default unchanged. The layer's replies are
// cassettes recorded by tools/e2e_layer.py for the current bundles: nothing is sent.
import { execFileSync } from "node:child_process";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, shot, test, type HubInfo } from "./fixtures";

type LayerHub = HubInfo & { home: string; research_root: string };
type Brief = { total: number; items: { group: string; id: string; title: string; why: string }[] };
type Unplaced = { claim_id: string; suggestion: { agree: boolean; question_id: string } | null };

const REPO = resolve(process.env.PROMETHEOS_REPO ?? join(fileURLToPath(new URL(".", import.meta.url)), "..", ".."));

/** The helper, as the agent's second process on the same store (like the CLI). */
function layer(hub: LayerHub, ...args: string[]): any {
  const out = execFileSync(
    "uv",
    ["run", "--offline", "--no-sync", "python", "tools/e2e_layer.py", ...args, "--home", hub.home, "--project", hub.project_id],
    { cwd: REPO, encoding: "utf-8", windowsHide: true, timeout: 120_000 },
  );
  return JSON.parse(out.trim().split("\n").pop()!);
}

test("Place it there writes one answers link with the claim's and the question's revisions", async ({
  paired: page,
  hub,
  api,
}) => {
  const info = hub as LayerHub;
  const create = (kind: string, id: string, body: unknown) => ({
    op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body,
  }); // prettier-ignore
  const question = `entry_${crypto.randomUUID()}`;
  const claim = `claim_${crypto.randomUUID()}`;
  const title = "Does regenerative braking pay back the flywheel's mass on a delivery van?";
  const statement = "A delivery van recovers more braking energy than its flywheel costs to carry.";
  await api.post("/transactions", {
    schema_version: 1,
    project_id: hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations: [
      create("entry", question, {
        title, text: title, category: "question", evidence: [], claim_refs: [], author_ref: null,
        origin: "typed", quantities: [], review: "reviewed",
      }), // prettier-ignore
      create("claim", claim, {
        statement,
        scope: {
          system: "Synthetic delivery van",
          operating_regime: "Urban delivery cycle",
          comparator: "Same van without the device",
          model_fidelity: "Reduced model",
          validation_level: "Not yet checked",
        },
        evidence: [], assessment_refs: [], state: "proposed", superseded_by: null,
      }), // prettier-ignore
    ],
  });
  const reply = JSON.stringify({ result: { question_id: question, quote: "recovers more braking energy" }, abstain: null });
  const ran = layer(info, "run", "--task", "map.link_claim_question", "--claim", claim, "--reply", `*=${reply}`);
  expect(ran.items.map((i: { outcome: string }) => i.outcome)).toEqual(["accepted"]);
  const map = await api.get<{ unplaced_claims: Unplaced[] }>("/knowledge/research-map");
  const listed = map.unplaced_claims.find((c) => c.claim_id === claim);
  expect(listed?.suggestion).toMatchObject({ agree: true, question_id: question });
  expect(layer(info, "relations", "--claim", claim).answers).toEqual([]);

  await page.goto(hub.url);
  const unplaced = page.getByTestId("map-unplaced-claims");
  const showAll = unplaced.getByRole("button", { name: /^Show all/ });
  if (await showAll.count()) await showAll.click();
  const row = unplaced.getByTestId("unplaced-claim").filter({ hasText: statement });
  const suggestion = row.getByTestId("claim-suggestion");
  await expect(suggestion).toContainText(title);
  await expect(suggestion).toContainText("two models agree, from a recording");
  await expect(suggestion).toContainText("recovers more braking energy");
  await shot(page, "30-place-it-there");
  const before = await api.seq();
  await suggestion.getByRole("button", { name: `Place it under ${title}` }).click();
  await expect(unplaced.getByTestId("unplaced-claim").filter({ hasText: statement })).toHaveCount(0);
  expect(await api.seq()).toBe(before + 1); // one owner act, one transaction
  expect(layer(info, "relations", "--claim", claim).answers).toEqual([
    { source_id: claim, source_rev: 1, target_id: question, target_rev: 1 },
  ]);
});

test("a rerun's result shows its draft reading beside it, with no tolerance", async ({ paired: page, hub, api }) => {
  const info = hub as LayerHub;
  const made = layer(info, "rerun", "--folder", info.research_root, "--name", "layer-run.log");
  const receipt = made.receipt.results.find((r: { id: string }) => r.id.startsWith("version_")).id as string;
  type Item = { id: string; drafts: { status: string; measured: { decimal: string } | null; tolerance: null; difference: string }[] };
  const before = await api.seq();
  const now = await api.get<{ awaiting_interpretation: { items: Item[] } }>("/now");
  const item = now.awaiting_interpretation.items.find((i) => i.id === receipt);
  expect(item?.drafts).toMatchObject([{ status: "drafted", measured: { decimal: "201.3" }, tolerance: null, difference: "4.4" }]);
  expect(await api.seq()).toBe(before); // the draft is a read

  await page.goto(hub.url);
  const row = page.getByTestId("now-awaiting").locator(`[data-testid="now-item"][data-id="${receipt}"]`);
  const draft = row.getByTestId("result-draft");
  await expect(draft).toHaveAttribute("data-status", "drafted");
  await expect(draft.getByTestId("draft-numbers")).toHaveText(
    "Expected 196.9 MJ, measured 201.3 MJ (line 2 of the new version): +4.4 MJ (+2.235 %)",
  );
  await expect(draft.getByTestId("draft-source")).toContainText("No tolerance is set");
  await row.getByText("Record an interpretation or dismiss with a reason").click();
  await row.getByTestId("resolve-from-draft").click();
  await expect(row.getByTestId("resolve-text")).toHaveValue(/^Expected 196\.9 MJ, measured 201\.3 MJ on line 2 .* Tolerance: $/);
  await shot(page, "30-result-draft");
  expect(await api.seq()).toBe(before); // nothing is recorded until the owner submits
});

test("a card shows the layer's wording with the bound number filled in, its default unchanged", async ({
  paired: page,
  hub,
  api,
}) => {
  const info = hub as LayerHub;
  const made = layer(info, "card", "--folder", info.research_root, "--name", "layer-card.log");
  const approval = made.approval_request_id as string;
  const reply = JSON.stringify({
    result: {
      what: "Withdraw the claim that the climb takes {q:q1}?",
      why: "The stored cycle used the steepest grade everywhere.",
      recommendation: "Apply the correction so views stop quoting it.",
    },
    abstain: null,
  });
  const ran = layer(info, "run", "--task", "cards.word", "--claim", made.claim_id, "--reply", `*=${reply}`);
  expect(ran.items.map((i: { outcome: string }) => i.outcome)).toEqual(["accepted"]);
  type Card = { approval_request_id: string; card: Record<string, string | null>; wording: { what: string; hint_only: boolean } | null };
  const cards = (await api.get<{ cards: Card[] }>("/decisions/cards")).cards;
  const stored = cards.find((c) => c.approval_request_id === approval)!;
  expect(stored.card).toEqual(made.card); // the agent's card, default and date untouched
  expect(stored.wording).toMatchObject({ what: "Withdraw the claim that the climb takes 196.9 MJ?", hint_only: true });

  await page.goto(hub.url);
  const card = page.getByTestId("decision-card").filter({ hasText: made.card.what });
  const words = card.getByTestId("card-wording");
  await expect(words).toContainText("Withdraw the claim that the climb takes 196.9 MJ?");
  await expect(words).toContainText("written by the intelligence layer; a hint");
  await expect(words).not.toContainText("{q:");
  await expect(card.getByTestId("card-default")).toHaveText(`If you don't answer: ${made.card.default_text}`);
  await shot(page, "30-card-wording");
});

// Last, so the rerun's result and the card above are in the brief: its order is always checked
// on two or more groups (review map-ai-spread-b R1-F9).
test("the brief opens Now in the API's order, each item with its reason", async ({ paired: page, hub, api }) => {
  const brief = (await api.get<{ brief: Brief }>("/now")).brief;
  expect(brief.items.length).toBeGreaterThanOrEqual(2);
  expect(new Set(brief.items.map((i) => i.group)).size).toBeGreaterThanOrEqual(2);
  await page.goto(hub.url);
  const section = page.getByTestId("now-brief");
  const items = section.getByTestId("brief-item");
  await expect(items).toHaveCount(brief.items.length);
  for (const [i, item] of brief.items.entries()) {
    await expect(items.nth(i)).toContainText(item.title);
    await expect(items.nth(i)).toHaveClass(new RegExp(`brief-${item.group}`));
  }
  await expect(section).toContainText("Computed from the record, not by a model.");
});
