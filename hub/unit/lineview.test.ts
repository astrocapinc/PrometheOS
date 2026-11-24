// Unit tests for the matrix line view's reader (CP11 S11.4; review round 2, R2-F6).
// Run: npm run test:unit (Node's built-in runner; Node strips the TypeScript types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { readLines, type VersionTextPage } from "../src/lineview.ts";

/** A version-text route over ``text`` answering pages of ``size`` characters. */
function server(text: string, size: number) {
  const offsets: number[] = [];
  const fetchPage = async (offset: number): Promise<VersionTextPage> => {
    offsets.push(offset);
    const shown = text.slice(offset, offset + size);
    const end = offset + shown.length;
    return {
      text: shown,
      reason: null,
      truncated: end < text.length,
      next_offset: end < text.length ? end : null,
    };
  };
  return { fetchPage, offsets };
}

const FILE = Array.from({ length: 50 }, (_, i) => `line ${i + 1}\r`).join("\n") + "\n";

test("lines on the first page are read from it alone", async () => {
  const { fetchPage, offsets } = server(FILE, 100_000);
  const out = await readLines(fetchPage, 3, 4);
  assert.deepEqual(out, { status: "ready", lines: ["line 3", "line 4"] });
  assert.deepEqual(offsets, [0]);
});

test("lines beyond a truncated first page are read from the next pages", async () => {
  const { fetchPage, offsets } = server(FILE, 40);
  const out = await readLines(fetchPage, 41, 42);
  assert.deepEqual(out, { status: "ready", lines: ["line 41", "line 42"] });
  assert.ok(offsets.length > 1);
  assert.equal(offsets[1], 40); // next_offset is followed
});

test("lines that cannot be reached are reported, not shown empty", async () => {
  const { fetchPage } = server(FILE, 40);
  const short = await readLines(fetchPage, 60, 61);
  assert.equal(short.status, "beyond");
  const capped = await readLines(server(FILE, 40).fetchPage, 41, 42, 100);
  assert.equal(capped.status, "beyond");
});

test("a version without retained text says why", async () => {
  const out = await readLines(async () => ({ text: null, reason: "not UTF-8 text" }), 1, 1);
  assert.deepEqual(out, { status: "unavailable", reason: "not UTF-8 text" });
});
