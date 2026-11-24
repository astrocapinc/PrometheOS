import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { boundaryProjects, boundaryUse } from "./e2e/boundary";

// C11-WALK (tools/walk_cp11.py): the CP11 walk's owner steps against the real hub on a fresh
// store the harness made. The harness starts the hub (tools/walk_hub18.py) and writes its
// address and tokens into environment variables; the walk runs as one ordered test (no retries:
// a retry could hide a real failure). Mid-way the test hands over to the agent's MCP session,
// which the harness runs while the hub keeps serving, and waits for it (WALK_SIGNALS).
// The browser is Playwright's own Chromium headless shell inside the same loopback-only boundary
// as the hub e2e suite (e2e/boundary.ts; decision 0028); walk11/00-boundary.spec.ts proves it
// against the walk's hub as the `boundary` project, a dependency of the walk itself.
export default defineConfig({
  testDir: "./walk11",
  testMatch: /.*\.spec\.ts$/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 15_000 },
  reporter: [["line"]],
  // Never inside the repository: a file Playwright writes there would change the candidate
  // tree while the walk runs. The harness passes a temporary folder.
  outputDir: process.env.WALK_PW_OUT ?? join(tmpdir(), "prometheos-walk11-playwright"),
  use: {
    ...boundaryUse,
    viewport: { width: 1280, height: 900 },
    // A step that cannot find its element fails in seconds with a screenshot, not after ten minutes.
    actionTimeout: 20_000,
    trace: "retain-on-failure",
  },
  projects: boundaryProjects(),
});
