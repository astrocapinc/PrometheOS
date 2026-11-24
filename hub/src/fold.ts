// Folding what repeats (decision 0078, from the KERS friction audit). Pure helpers, unit-tested
// in unit/fold.test.ts: the pages decide what to show, these decide what belongs together.
import type { NowItem, NowSection } from "./types";

/** Suggestions fold by first label only when the list is long, and only for a label with at
 * least FOLD_AT of them: an adoption plan files dozens; a short list stays as it is. */
export const FOLD_AT = 5;
export const FOLD_WHEN_MORE_THAN = 10;

export interface Labelled {
  body: { labels?: string[]; due_at?: string | null };
}

/** Suggestions grouped by first label, in first-seen order: the loose ones stay listed, the
 * large groups fold. */
export function foldSuggestions<T extends Labelled>(items: T[]): { loose: T[]; folded: [string | null, T[]][] } {
  if (items.length <= FOLD_WHEN_MORE_THAN) return { loose: items, folded: [] };
  const groups = new Map<string | null, T[]>();
  const loose: T[] = [];
  for (const item of items) {
    if (item.body.due_at) {
      loose.push(item); // a suggestion with a deadline is never folded away (review F7)
      continue;
    }
    const label = item.body.labels?.[0] ?? null;
    groups.set(label, [...(groups.get(label) ?? []), item]);
  }
  const folded: [string | null, T[]][] = [];
  for (const [label, group] of groups) {
    if (group.length >= FOLD_AT) folded.push([label, group]);
    else loose.push(...group);
  }
  return { loose, folded };
}

/** Now's "changed" holds records a change flagged for review and results an agent's assessment
 * cleared without the owner's review. Only the first is a chore; the second stays visible,
 * grouped and worded as what it is (decisions 0018, 0078). */
export function splitChanged(section: NowSection): {
  flagged: NowSection;
  checked: NowItem[];
  checkedTotal: number;
} {
  const checked = section.items.filter((i) => i.checked_by_agent);
  // The server counts the whole list; a truncated page holds only some of it (review F1).
  const checkedTotal = section.checked_by_agent ?? checked.length;
  return {
    flagged: { ...section, items: section.items.filter((i) => !i.checked_by_agent), count: section.count - checkedTotal },
    checked,
    checkedTotal,
  };
}
