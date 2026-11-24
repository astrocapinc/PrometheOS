// The boundary evidence rules (decision 0028), without a browser: a control that failed once never
// reports all_passed, even when a retry passes it; a control that never ran keeps it false.
import { BOUNDARY_ARGS } from "./boundary";
import { emptyEvidence, mergeOutcome } from "./boundary-evidence";
import { expect, test } from "./fixtures";

const registered = ["control 1", "control 2"] as const;
const outcome = (title: string, status: string, observed: Record<string, unknown> = {}) => ({
  title,
  status,
  observed,
  registered,
});

test("all_passed needs every registered control to have passed", () => {
  let evidence = emptyEvidence(BOUNDARY_ARGS);
  evidence = mergeOutcome(evidence, outcome("control 1", "passed", { a: 1 }));
  expect(evidence.all_passed).toBe(false); // control 2 has not run
  evidence = mergeOutcome(evidence, outcome("control 2", "passed", { b: 2 }));
  expect(evidence.all_passed).toBe(true);
  expect(evidence.observed).toEqual({ a: 1, b: 2 });
});

test("a failure is sticky: a retry that passes does not turn it into a pass", () => {
  let evidence = emptyEvidence(BOUNDARY_ARGS);
  evidence = mergeOutcome(evidence, outcome("control 1", "passed"));
  evidence = mergeOutcome(evidence, outcome("control 2", "failed"));
  expect(evidence.all_passed).toBe(false);
  // The retry: every control passes the second time.
  evidence = mergeOutcome(evidence, outcome("control 1", "passed"));
  evidence = mergeOutcome(evidence, outcome("control 2", "passed"));
  expect(evidence.controls).toEqual([
    { control: "control 1", status: "passed" },
    { control: "control 2", status: "failed" },
  ]);
  expect(evidence.all_passed).toBe(false);
});

test("a timed out or interrupted control also stays not passed", () => {
  let evidence = emptyEvidence(BOUNDARY_ARGS);
  evidence = mergeOutcome(evidence, outcome("control 1", "timedOut"));
  evidence = mergeOutcome(evidence, outcome("control 1", "passed"));
  evidence = mergeOutcome(evidence, outcome("control 2", "passed"));
  expect(evidence.controls.find((c) => c.control === "control 1")?.status).toBe("timedOut");
  expect(evidence.all_passed).toBe(false);
});
