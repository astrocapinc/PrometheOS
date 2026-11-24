// Reading the cited lines of a retained text version for the matrix line view (A02, CP11 S11.4;
// review F10, round 2 R2-F6). The version-text route answers one page at a time; a long
// bibliography file is truncated after the first page, so the pages are read (next_offset)
// until the cited lines are covered, or the reader says the lines could not be reached.
// Lines are split on LF with a trailing CR removed, as the server's text locators count them.

export interface VersionTextPage {
  text: string | null;
  reason: string | null;
  truncated?: boolean;
  next_offset?: number | null;
}

export type LinesResult =
  | { status: "unavailable"; reason: string }
  | { status: "ready"; lines: string[] }
  | { status: "beyond"; read: number };

/** At most this much text is read to reach the cited lines (a bibliography file is at most 8 MiB). */
export const MAX_LINE_VIEW_CHARS = 8 * 1024 * 1024;

export async function readLines(
  fetchPage: (offset: number) => Promise<VersionTextPage>,
  startLine: number,
  endLine: number,
  maxChars: number = MAX_LINE_VIEW_CHARS,
): Promise<LinesResult> {
  let text = "";
  let offset = 0;
  for (;;) {
    const page = await fetchPage(offset);
    if (page.text === null) return { status: "unavailable", reason: page.reason ?? "no text" };
    text += page.text;
    // More than endLine pieces means line endLine is complete (its LF has been read).
    const covered = text.split("\n").length > endLine;
    if (covered || !page.truncated || page.next_offset == null) break;
    if (text.length >= maxChars) return { status: "beyond", read: text.length };
    offset = page.next_offset;
  }
  const all = text.split("\n");
  if (all.length < endLine) return { status: "beyond", read: text.length };
  const lines = all.slice(startLine - 1, endLine).map((line) => line.replace(/\r$/, ""));
  return { status: "ready", lines };
}
