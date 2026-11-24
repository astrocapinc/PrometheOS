import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { boundaryProjects, boundaryUse } from "./e2e/boundary";

// C10-WALK (tools/walk_cp10.py): the CP10 walk against the real hub on a store the harness
// seeded from the synthetic fixtures. The harness starts the hub (tools/walk_hub.py) and writes
// its address, tokens and the seeded IDs into environment variables; there is no global setup
// here, and the walk runs as one ordered test (no retries: a retry could hide a real failure).
// The browser is Playwright's own Chromium headless shell inside the same loopback-only boundary
// as the hub e2e suite (e2e/boundary.ts; decision 0028); walk/00-boundary.spec.ts proves it against
// the walk's hub as the `boundary` project, a dependency of the walk itself.
export default defineConfig({
  testDir: "./walk",
  testMatch: /.*\.spec\.ts$/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: [["line"]],
  // Never inside the repository: a file Playwright writes there would change the candidate
  // tree while the walk runs (review cp10-6-a F3). The harness passes a temporary folder.
  outputDir: process.env.WALK_PW_OUT ?? join(tmpdir(), "prometheos-walk-playwright"),
  use: {
    ...boundaryUse,
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
  },
  projects: boundaryProjects(),
});
