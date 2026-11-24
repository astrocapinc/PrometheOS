// Data helpers shared by pages: kind lists, record titles and record links.
import { useEffect, useState } from "react";
import { ApiError, get } from "./api";
import { useHub, useQuery } from "./hub";
import type { Envelope, RecordList } from "./types";

export function useKindList<B>(kind: string) {
  return useQuery<RecordList<B>>(`/records?kind=${encodeURIComponent(kind)}&limit=200`);
}

/** One plain-language line that identifies a record. */
export function recordTitle(record: Envelope): string {
  const b = record.body as Record<string, unknown>;
  const pick = (key: string) => (typeof b[key] === "string" ? (b[key] as string) : "");
  switch (record.kind) {
    case "entry":
    case "meeting":
    case "action":
    case "source":
    case "project":
      return pick("title") || record.kind;
    case "claim":
      return pick("statement");
    case "correction":
      return pick("reason");
    case "item":
      return pick("text");
    case "task":
      return `Agent task: ${pick("purpose").replace(/_/g, " ")}`;
    default:
      return pick("title") || pick("name") || record.kind;
  }
}

/** Where a record is shown in the hub (null when it has no own page). */
export function recordPath(record: Pick<Envelope, "id" | "kind" | "body">): string | null {
  switch (record.kind) {
    case "meeting":
      return `/meetings/${record.id}`;
    case "item": {
      const meeting = (record.body as { meeting_id?: string | null }).meeting_id;
      return meeting ? `/meetings/${meeting}` : null;
    }
    case "correction":
      return `/corrections/${record.id}`;
    case "action":
      return `/actions/${record.id}`;
    case "entry":
      return `/notebook#${record.id}`;
    case "claim":
      return `/knowledge#${record.id}`;
    case "run":
      return `/workflows/${record.id}`;
    case "paper":
      return `/papers/${record.id}`;
    case "anchor": {
      const paper = (record.body as { paper_id?: string }).paper_id;
      return paper ? `/papers/${paper}#${record.id}` : null;
    }
    default:
      return null;
  }
}

/** Fetch several records by ID (missing/purged ones are skipped). */
export function useRecords(ids: string[]): {
  records: Map<string, Envelope>;
  loading: boolean;
  error: unknown;
} {
  const { dataVersion, status } = useHub();
  const key = ids.join(",");
  const [state, setState] = useState<{ records: Map<string, Envelope>; loading: boolean; error: unknown }>(
    { records: new Map(), loading: ids.length > 0, error: null },
  );
  useEffect(() => {
    if (status !== "paired") return;
    const list = key ? key.split(",") : [];
    if (list.length === 0) {
      setState({ records: new Map(), loading: false, error: null });
      return;
    }
    const controller = new AbortController();
    setState((s) => ({ ...s, loading: true }));
    Promise.all(
      list.map((id) =>
        get<Envelope>(`/records/${encodeURIComponent(id)}`, { signal: controller.signal }).catch(
          (err: unknown) => {
            if (err instanceof ApiError && err.kind === "not_found") return null;
            throw err;
          },
        ),
      ),
    )
      .then((found) => {
        const map = new Map<string, Envelope>();
        for (const r of found) if (r) map.set(r.id, r);
        setState({ records: map, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.kind === "aborted") return;
        setState((s) => ({ ...s, loading: false, error: err }));
      });
    return () => controller.abort();
  }, [key, dataVersion, status]);
  return state;
}

const LAST_SEEN_PREFIX = "prometheos.hub.lastSeenSeq.";

/** Non-credential UI state: the project sequence the user last marked as seen. */
export function readLastSeen(projectId: string): number | null {
  try {
    const raw = window.localStorage.getItem(LAST_SEEN_PREFIX + projectId);
    if (raw === null) return null;
    const value = Number.parseInt(raw, 10);
    return Number.isFinite(value) && value >= 0 ? value : null;
  } catch {
    return null;
  }
}

export function writeLastSeen(projectId: string, seq: number): void {
  try {
    window.localStorage.setItem(LAST_SEEN_PREFIX + projectId, String(seq));
  } catch {
    // Storage unavailable: the "since last visit" view simply starts from the beginning.
  }
}
