// Unit tests for work-item deadlines (A05, CP18.1; review s1-a F2/F4). Run: npm run test:unit
// (Node's built-in runner; Node strips the TypeScript types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { dueNote, formatDeadline, parseLabels, labelProblem, sameWallTime, utcOffset, utcToZoned, zonedText, zonedToUtc } from "../src/deadline.ts";

test("fixed-offset zone: wall time to UTC and back", () => {
  assert.equal(zonedToUtc("2026-11-02", "23:59", "Asia/Riyadh"), "2026-11-02T20:59:00Z");
  assert.deepEqual(utcToZoned("2026-11-02T20:59:00Z", "Asia/Riyadh"), { date: "2026-11-02", time: "23:59" });
  assert.equal(formatDeadline("2026-11-02T20:59:00Z", "Asia/Riyadh"), "Mon, 2 Nov 2026, 23:59 (Asia/Riyadh, UTC+03:00)");
});

for (const [zone, date, iso] of [
  ["America/New_York", "2026-03-07", "2026-03-07T15:00:00Z"], // day before spring forward (EST)
  ["America/New_York", "2026-03-09", "2026-03-09T14:00:00Z"], // day after (EDT)
  ["America/New_York", "2026-10-31", "2026-10-31T14:00:00Z"], // day before fall back (EDT)
  ["America/New_York", "2026-11-02", "2026-11-02T15:00:00Z"], // day after (EST)
  ["Europe/London", "2026-03-28", "2026-03-28T10:00:00Z"],
  ["Europe/London", "2026-03-30", "2026-03-30T09:00:00Z"],
  ["Europe/London", "2026-10-24", "2026-10-24T09:00:00Z"],
  ["Europe/London", "2026-10-26", "2026-10-26T10:00:00Z"],
] as const) {
  test(`${zone} ${date} 10:00 around a daylight-saving change`, () => {
    assert.equal(zonedToUtc(date, "10:00", zone), iso);
    assert.deepEqual(utcToZoned(iso, zone), { date, time: "10:00" });
  });
}

test("a repeated wall time (clocks go back) is the earlier instant in every zone", () => {
  // New York 2026-11-01 01:30 occurs at 05:30Z (EDT) and 06:30Z (EST).
  assert.equal(zonedToUtc("2026-11-01", "01:30", "America/New_York"), "2026-11-01T05:30:00Z");
  // London 2026-10-25 01:30 occurs at 00:30Z (BST) and 01:30Z (GMT).
  assert.equal(zonedToUtc("2026-10-25", "01:30", "Europe/London"), "2026-10-25T00:30:00Z");
  // Berlin 2026-10-25 02:30 occurs at 00:30Z (CEST) and 01:30Z (CET).
  assert.equal(zonedToUtc("2026-10-25", "02:30", "Europe/Berlin"), "2026-10-25T00:30:00Z");
  // The offset in the display tells the two occurrences apart.
  assert.match(formatDeadline("2026-11-01T05:30:00Z", "America/New_York"), /01:30 \(America\/New_York, UTC-04:00\)$/);
  assert.match(formatDeadline("2026-11-01T06:30:00Z", "America/New_York"), /01:30 \(America\/New_York, UTC-05:00\)$/);
});

test("a skipped wall time (clocks go forward) moves forward by the gap in every zone", () => {
  // New York 2026-03-08 02:30 does not exist; it becomes 03:30 EDT (07:30Z).
  const ny = zonedToUtc("2026-03-08", "02:30", "America/New_York");
  assert.equal(ny, "2026-03-08T07:30:00Z");
  assert.deepEqual(utcToZoned(ny, "America/New_York"), { date: "2026-03-08", time: "03:30" });
  // London 2026-03-29 01:30 does not exist; it becomes 02:30 BST (01:30Z).
  const ldn = zonedToUtc("2026-03-29", "01:30", "Europe/London");
  assert.equal(ldn, "2026-03-29T01:30:00Z");
  assert.deepEqual(utcToZoned(ldn, "Europe/London"), { date: "2026-03-29", time: "02:30" });
});

test("half-hour offsets", () => {
  assert.equal(zonedToUtc("2026-11-02", "09:00", "Asia/Kolkata"), "2026-11-02T03:30:00Z");
  assert.equal(utcOffset("2026-11-02T03:30:00Z", "Asia/Kolkata"), "UTC+05:30");
});

test("an untouched deadline is recognised, so an edit keeps the stored value", () => {
  // A stored deadline with seconds, or in a repeated hour, shows as the same wall time.
  assert.ok(sameWallTime("2026-11-02T20:59:30Z", "2026-11-02", "23:59", "Asia/Riyadh"));
  assert.ok(sameWallTime("2026-11-01T06:30:00Z", "2026-11-01", "01:30", "America/New_York"));
  assert.ok(!sameWallTime("2026-11-02T20:59:00Z", "2026-11-03", "23:59", "Asia/Riyadh"));
});

test("labels: parsing and the server's character rules", () => {
  assert.deepEqual(parseLabels(" paper P1, submission,, Paper P1 "), ["paper P1", "submission"]);
  assert.equal(labelProblem(["ok"]), null);
  assert.match(labelProblem(["x".repeat(41)]) ?? "", /at most 40 characters/);
  assert.match(labelProblem(["paper\u202eP1"]) ?? "", /text-direction/);
  assert.match(labelProblem(["c1\u0085"]) ?? "", /control/);
  assert.match(labelProblem(Array.from({ length: 17 }, (_, i) => `l${i}`)) ?? "", /16 labels/);
});

// Decision 0048: a closed item's deadline no longer counts down.
test("done and cancelled items show no countdown; open ones do", () => {
  const now = Date.parse("2026-10-15T09:00:00Z");
  const due = "2026-10-20T09:00:00Z";
  assert.equal(dueNote(due, now), "5 days left");
  assert.equal(dueNote(due, now, "in_progress"), "5 days left");
  assert.equal(dueNote(due, now, "done"), null);
  assert.equal(dueNote(due, now, "cancelled"), null);
  assert.equal(dueNote("2026-10-01T09:00:00Z", now, "done"), null); // not "deadline passed"
});

// Decision 0048: Now's reasons carry UTC stamps from the API; the hub shows them in the zone.
test("UTC stamps inside a reason are shown in the project's zone", () => {
  assert.equal(
    zonedText("Overdue since 2026-10-20T09:00:00Z; blocked: waiting", "Asia/Riyadh"),
    "Overdue since Tue, 20 Oct 2026, 12:00 (Asia/Riyadh, UTC+03:00); blocked: waiting",
  );
  assert.equal(
    zonedText("Reported running at 2026-10-15T08:05:30.123Z by agent.", "Europe/London"),
    "Reported running at Thu, 15 Oct 2026, 09:05 (Europe/London, UTC+01:00) by agent.",
  );
  assert.equal(zonedText("No stamp here; 2026-10-20 is a date only.", "UTC"), "No stamp here; 2026-10-20 is a date only.");
});
