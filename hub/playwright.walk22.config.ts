import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { boundaryProjects, boundaryUse } from "./e2e/boundary";

// C22-WALK (tools/walk_cp22.py; decision 0099): the CP22 walk's owner steps in the hub that
// `prometheos start` launched for a fresh home the harness made, at the home's own host name.
// The harness runs the launcher, the owner's terminal acts and the agent's MCP sessions, and
// hands over through signal files (WALK_SIGNALS); the walk runs as one ordered test (no
// retries: a retry could hide a real failure). The browser is Playwright's own Chromium headless
// shell inside the loopback-only boundary of the other suites (e2e/boundary.ts; decision 0028),
// which admits the home's host name by name only (PROMETHEOS_BOUNDARY_HOME_HOST);
// walk22/00-boundary.spec.ts proves it against the walk's hub as the `boundary` project.
export default defineConfig({
  testDir: "./walk22",
  testMatch: /.*\.spec\.ts$/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 15_000 },
  reporter: [["line"]],
  // Never inside the repository: a file Playwright writes there would change the candidate
  // tree while the walk runs. The harness passes a temporary folder.
  outputDir: process.env.WALK_PW_OUT ?? join(tmpdir(), "prometheos-walk22-playwright"),
  use: {
    ...boundaryUse,
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
  },
  projects: boundaryProjects(),
});
