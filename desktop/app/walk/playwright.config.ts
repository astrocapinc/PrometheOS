import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// C19-WALK's desktop leg (decision 0055): run by tools/walk_cp19.py, never on its own. One
// worker, no retries (a retry could hide a real failure); output outside the repository.
export default defineConfig({
  testDir: ".",
  testMatch: /walk19\.spec\.ts$/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 900_000,
  expect: { timeout: 30_000 },
  reporter: [["line"]],
  outputDir: process.env.WALK_PW_OUT ?? join(tmpdir(), "prometheos-walk19-playwright"),
});
