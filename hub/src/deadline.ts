// Work-item deadlines (A05, CP18.1; C18-ITEMS): stored in UTC, shown in the owner's time zone
// with the zone named. The owner's zone is the project's configured IANA time zone, or the
// browser's when the project has none.
import type { ProjectInfo } from "./types";

export function ownerZone(project: ProjectInfo | null): string {
  return project?.settings.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

interface Parts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

function parts(ms: number, zone: string): Parts {
  const out: Parts = { year: 1970, month: 1, day: 1, hour: 0, minute: 0, second: 0 };
  for (const p of new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(ms))) {
    if (p.type in out) out[p.type as keyof Parts] = Number(p.value);
  }
  return out;
}

/** Milliseconds the zone is ahead of UTC at this instant. */
function offset(ms: number, zone: string): number {
  const p = parts(ms, zone);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
}

const DAY = 86_400_000;

/** The UTC instant of a wall-clock date ("2026-11-02") and time ("23:59") in a zone, as the
 *  stored form ("2026-11-02T20:59:00Z"). Daylight-saving policy, the same in every zone
 *  (decision 0034): a wall time that occurs twice (clocks go back) is the EARLIER instant; a
 *  wall time that does not exist (clocks go forward) moves forward by the gap, so 02:30 on a
 *  spring-forward night with a one-hour gap becomes 03:30. */
export function zonedToUtc(date: string, time: string, zone: string): string {
  const [y = 1970, m = 1, d = 1] = date.split("-").map(Number);
  const [h = 0, mi = 0] = time.split(":").map(Number);
  const wall = Date.UTC(y, m - 1, d, h, mi);
  // The offsets in force a day before and a day after bracket any transition on this date.
  const before = offset(wall - DAY, zone);
  const after = offset(wall + DAY, zone);
  // Candidates that really show this wall time in the zone (two in a repeated hour, none in a gap).
  const matches = [wall - before, wall - after].filter((t) => t + offset(t, zone) === wall);
  const chosen = matches.length ? Math.min(...matches) : wall - before; // gap: use the earlier offset
  return new Date(chosen).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** The wall-clock fields an instant shows in a zone; keeps an untouched deadline unchanged. */
export function sameWallTime(iso: string, date: string, time: string, zone: string): boolean {
  const shown = utcToZoned(iso, zone);
  return shown.date === date && shown.time === time;
}

/** The zone's UTC offset at an instant, as "UTC+03:00" or "UTC-04:00". */
export function utcOffset(iso: string, zone: string): string {
  const minutes = Math.round(offset(Date.parse(iso), zone) / 60_000);
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

/** The wall-clock date and time of a stored instant in a zone (for editing). */
export function utcToZoned(iso: string, zone: string): { date: string; time: string } {
  const p = parts(Date.parse(iso), zone);
  const two = (n: number) => String(n).padStart(2, "0");
  return { date: `${p.year}-${two(p.month)}-${two(p.day)}`, time: `${two(p.hour)}:${two(p.minute)}` };
}

/** "Mon, 2 Nov 2026, 23:59 (Europe/Istanbul, UTC+03:00)": the offset tells the two
 *  occurrences of a repeated hour apart. */
export function formatDeadline(iso: string, zone: string): string {
  const text = new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(iso));
  return `${text} (${zone}, ${utcOffset(iso, zone)})`;
}

/** Whole days from now to the deadline, rounded down; negative once it has passed. */
export function daysUntil(iso: string, now: number = Date.now()): number {
  return Math.floor((Date.parse(iso) - now) / 86_400_000);
}

/** Item states where a deadline no longer counts down (decision 0048). */
export const CLOSED_STATES: ReadonlySet<string> = new Set(["done", "cancelled"]);

/** A plain statement of where the deadline stands, never a call to act. A closed item's
 * deadline is history: no "days left" and no "deadline passed" (decision 0048). */
export function dueNote(iso: string, now: number = Date.now(), status?: string): string | null {
  if (status !== undefined && CLOSED_STATES.has(status)) return null;
  const ms = Date.parse(iso) - now;
  if (ms < 0) return "deadline passed";
  const days = Math.floor(ms / 86_400_000);
  if (days === 0) return "due within a day";
  return days === 1 ? "1 day left" : `${days} days left`;
}

// A UTC stamp as the API writes it inside a reason ("Overdue since 2026-10-20T09:00:00Z").
const UTC_STAMP = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z\b/g;

/** A reason with every UTC stamp in it shown in the project's zone (the API keeps UTC as data;
 * the hub formats times, decision 0035 section 10; decision 0048). */
export function zonedText(text: string, zone: string): string {
  return text.replace(UTC_STAMP, (stamp) => formatDeadline(stamp, zone));
}

/** Labels typed as "a, b, c": trimmed, empty ones dropped, duplicates (ignoring case) removed. */
export function parseLabels(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(",")) {
    const label = raw.trim();
    if (!label || seen.has(label.toLowerCase())) continue;
    seen.add(label.toLowerCase());
    out.push(label);
  }
  return out;
}

// eslint-disable-next-line no-control-regex
const FORBIDDEN_IN_LABEL = /[\u0000-\u001f\u007f-\u009f\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/;

/** Why a label list would be refused, in words, or null (mirrors the server's rules). */
export function labelProblem(labels: string[]): string | null {
  if (labels.length > 16) return "Use at most 16 labels.";
  const long = labels.find((l) => l.length > 40);
  if (long) return `Labels are at most 40 characters: “${long.slice(0, 50)}”.`;
  // The server's rule (domain/bodies.py WorkLabel): no control, separator or text-direction
  // characters. The server also treats labels as duplicates ignoring case (casefold) and decides.
  return labels.some((l) => FORBIDDEN_IN_LABEL.test(l))
    ? "Labels cannot contain control, line-separator or text-direction characters."
    : null;
}
