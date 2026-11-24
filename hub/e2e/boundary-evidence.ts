// The boundary evidence file (decision 0028): what the controls observed and how each ended, and
// which browser each journey worker was launched as. `tools/run_hub_e2e.py` and
// `tools/walk_cp10.py` name the file in BOUNDARY_EVIDENCE, delete any earlier one, and fail the
// run unless it shows every control passed. No `test` import: this is plain data handling.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface JourneyBrowser {
  project: string;
  worker: number;
  version: string;
  image: string;
}

export interface Evidence {
  decision: string;
  launch_args: readonly string[];
  observed: Record<string, unknown>;
  controls: { control: string; status: string }[];
  all_passed: boolean;
  journeys: JourneyBrowser[];
}

export function emptyEvidence(launchArgs: readonly string[]): Evidence {
  return { decision: "0028", launch_args: launchArgs, observed: {}, controls: [], all_passed: false, journeys: [] };
}

/**
 * Fold one control's outcome into the evidence. A control that was ever not `passed` stays that
 * way: a retry (or a repeat) that passes later must not turn a failure into a pass, so the status
 * of a control only ever moves away from `passed`. `all_passed` is true only when every
 * registered control has a `passed` entry.
 */
export function mergeOutcome(
  previous: Evidence,
  outcome: { title: string; status: string; observed: Record<string, unknown>; registered: readonly string[] },
): Evidence {
  const earlier = previous.controls.find((c) => c.control === outcome.title);
  const status = earlier && earlier.status !== "passed" ? earlier.status : outcome.status;
  const controls = previous.controls.filter((c) => c.control !== outcome.title).concat({ control: outcome.title, status });
  return {
    ...previous,
    observed: { ...previous.observed, ...outcome.observed },
    controls,
    all_passed: outcome.registered.every((name) => controls.some((c) => c.control === name && c.status === "passed")),
  };
}

export function readEvidence(target: string, launchArgs: readonly string[]): Evidence {
  try {
    return { ...emptyEvidence(launchArgs), ...(JSON.parse(readFileSync(target, "utf-8")) as Partial<Evidence>) };
  } catch {
    return emptyEvidence(launchArgs);
  }
}

export function writeEvidence(target: string, evidence: Evidence): void {
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, JSON.stringify(evidence, null, 2), "utf-8");
}
