// Unit tests for the library's code-point offsets in the hub (CP11 S11.1; review F4).
// Run: npm run test:unit (Node's built-in runner; Node strips the TypeScript types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { codePointLength, splitAtCodePoints } from "../src/codepoints.ts";

test("an astral character before the span counts as one code point", () => {
  const text = "\u{1D465} = ﬁnd the drag coefficient"; // mathematical italic x, then a ligature
  assert.equal(text.length, 29); // UTF-16 units: the italic x takes two
  assert.equal(codePointLength(text), 28); // what the server's offsets count
  const start = 12; // "drag coefficient" in code points, as the server reports it
  const [before, span, after] = splitAtCodePoints(text, start, start + 16);
  assert.equal(span, "drag coefficient");
  assert.equal(before, "\u{1D465} = ﬁnd the ");
  assert.equal(after, "");
  // String.slice with the same numbers is off by one: the bug this helper prevents.
  assert.notEqual(text.slice(start, start + 16), "drag coefficient");
});

test("offsets are clamped to the text", () => {
  assert.deepEqual(splitAtCodePoints("abc", 2, 99), ["ab", "c", ""]);
  assert.deepEqual(splitAtCodePoints("abc", 5, 9), ["abc", "", ""]);
});
