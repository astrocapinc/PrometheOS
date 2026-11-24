import { defineConfig } from "@playwright/test";
import { boundaryProjects, boundaryUse } from "./e2e/boundary";

// Browser tests run against the REAL hub (work11/tools/hub_fixture.py) serving this
// folder's dist build. The fixture project is shared and mutated by the journeys, so tests
// run in one worker, in file order, without retries (a retry could hide a real failure).
// The browser is Playwright's own Chromium headless shell, confined to loopback by the boundary
// shared with the C10-WALK (e2e/boundary.ts; decision 0028). e2e/00-boundary.spec.ts proves it with
// negative controls as the `boundary` project, a dependency of the journeys: it runs before them
// whatever spec or --grep filter is given, and the journeys do not run if a control fails.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  globalSetup: "./e2e/global-setup.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    ...boundaryUse,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: boundaryProjects(),
});
