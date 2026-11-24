// Library offsets count Unicode code points of the stored page text (A02, CP11 S11.1; decision
// 0061 section 7). JavaScript strings index UTF-16 code units, so a character above U+FFFF (a
// mathematical italic letter, an emoji) counts once in an offset but twice in String.slice.
// These helpers convert, so the hub highlights exactly the span whose digest it shows.

/** ``text`` split at code-point offsets ``start`` and ``end`` (end exclusive): before, span,
 *  after. Offsets are clamped to the text. */
export function splitAtCodePoints(text: string, start: number, end: number): [string, string, string] {
  const points = Array.from(text);
  const a = Math.max(0, Math.min(start, points.length));
  const b = Math.max(a, Math.min(end, points.length));
  return [points.slice(0, a).join(""), points.slice(a, b).join(""), points.slice(b).join("")];
}

/** The length of ``text`` in code points. */
export function codePointLength(text: string): number {
  return Array.from(text).length;
}
