// Where the hub's pages live: the sidebar, the command palette and the "g then letter"
// shortcuts all read this one list.
import type { IconName } from "./components/icons";

export interface NavEntry {
  to: string;
  label: string;
  icon: IconName;
  /** The letter after "g" that opens this page. */
  key?: string;
  /** Extra words the palette matches. */
  keywords?: string;
}

/** The primary destinations (spec/06), plus work items (A05). */
export const DESTINATIONS: NavEntry[] = [
  { to: "/now", label: "Now", icon: "now", key: "n", keywords: "home inbox changes attention" },
  { to: "/work", label: "Work items", icon: "work", key: "w", keywords: "tasks deadlines todo" },
  { to: "/notebook", label: "Notebook", icon: "notebook", key: "b", keywords: "entries journal results" },
  { to: "/knowledge", label: "Knowledge", icon: "knowledge", key: "k", keywords: "claims findings evidence" },
  { to: "/papers", label: "Papers", icon: "papers", key: "p", keywords: "manuscript latex deliverable" },
];

/** Contextual pages reached from the sidebar's Operate and System groups. */
export const OPERATE: NavEntry[] = [
  { to: "/approvals", label: "Approvals", icon: "approvals", key: "a", keywords: "requests grant deny agent" },
  { to: "/runs", label: "Runs", icon: "runs", key: "r", keywords: "operations progress jobs" },
  { to: "/adoption", label: "Adoption", icon: "adoption", key: "d", keywords: "scan families baseline folder" },
  { to: "/artifacts", label: "Artifacts", icon: "artifacts", keywords: "backup coverage availability" },
  { to: "/skills", label: "Skills", icon: "skills", keywords: "packages workflows recipes" },
];

export const SYSTEM: NavEntry[] = [
  { to: "/ai", label: "AI provider", icon: "ai", keywords: "model provider transcription settings" },
  { to: "/recovery", label: "Recovery", icon: "recovery", keywords: "backup restore health" },
];

/** Pages the palette can open that the sidebar reaches through a parent page. */
export const MORE_PAGES: NavEntry[] = [
  { to: "/knowledge/library", label: "Library", icon: "library", key: "l", keywords: "pdf papers literature zotero" },
  { to: "/knowledge/matrix", label: "Evidence matrices", icon: "matrix", key: "m", keywords: "matrix comparison" },
  { to: "/capture", label: "Capture", icon: "plus", key: "c", keywords: "note meeting recording file" },
];

export const ALL_PAGES: NavEntry[] = [...DESTINATIONS, ...OPERATE, ...SYSTEM, ...MORE_PAGES];

export function isCurrent(to: string, path: string): boolean {
  const current = path === "/" ? "/now" : path;
  return current === to || current.startsWith(`${to}/`);
}
