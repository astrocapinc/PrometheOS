// Unit tests for folding what repeats (decision 0078). Run: npm run test:unit
import assert from "node:assert/strict";
import { test } from "node:test";
import { FOLD_AT, FOLD_WHEN_MORE_THAN, foldSuggestions, splitChanged } from "../src/fold.ts";

const item = (label: string | null, n: number) => ({ id: `${label}-${n}`, body: { labels: label ? [label] : [] } });

test("a short list of suggestions is never folded", () => {
  const items = Array.from({ length: FOLD_WHEN_MORE_THAN }, (_, n) => item("adoption", n));
  assert.deepEqual(foldSuggestions(items), { loose: items, folded: [] });
});

test("a long list folds each label with enough suggestions; the rest stay listed", () => {
  const plan = Array.from({ length: 60 }, (_, n) => item("adoption", n));
  const few = Array.from({ length: FOLD_AT - 1 }, (_, n) => item("meeting", n));
  const bare = Array.from({ length: FOLD_AT }, (_, n) => item(null, n));
  const { loose, folded } = foldSuggestions([...few, ...plan, ...bare]);
  assert.deepEqual(loose.map((i) => i.id), few.map((i) => i.id));
  assert.deepEqual(folded.map(([label, group]) => [label, group.length]), [["adoption", 60], [null, FOLD_AT]]);
});

test("Now's changed list keeps agent-checked results apart and out of the review count", () => {
  const flagged = { id: "claim_a", kind: "claim", title: "A", reason: "Needs review" };
  const checked = { id: "version_b", kind: "receipt", title: "R2", reason: "Checked", checked_by_agent: true, outcomes: ["supports"] };
  const split = splitChanged({ count: 2, items: [flagged, checked], truncated: false });
  assert.equal(split.flagged.count, 1);
  assert.deepEqual(split.flagged.items, [flagged]);
  assert.deepEqual(split.checked, [checked]);
});

test("a suggestion with a deadline never folds away", () => {
  const plan = Array.from({ length: 12 }, (_, n) => item("adoption", n));
  const dated = { id: "dated", body: { labels: ["adoption"], due_at: "2026-11-05T09:00:00Z" } };
  const { loose, folded } = foldSuggestions([...plan, dated]);
  assert.deepEqual(loose.map((i) => i.id), ["dated"]);
  assert.equal(folded[0]![1].length, 12);
});

test("a truncated changed list still counts every agent-checked result", () => {
  const flagged = Array.from({ length: 10 }, (_, n) => ({ id: `c${n}`, kind: "claim", title: "", reason: "" }));
  const checked = Array.from({ length: 40 }, (_, n) => ({ id: `v${n}`, kind: "receipt", title: "", reason: "", checked_by_agent: true }));
  const split = splitChanged({ count: 110, items: [...flagged, ...checked], truncated: true, checked_by_agent: 90 });
  assert.equal(split.checkedTotal, 90);
  assert.equal(split.flagged.count, 20);
  assert.equal(split.checked.length, 40);
});
