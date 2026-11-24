import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The desktop shell's own tests (CP19.2, decision 0054): Playwright's Electron driver launches
// the real app against a fresh synthetic home. One worker, no retries (a retry could hide a real
// failure); output outside the repository so a run never changes the tree.
export default defineConfig({
  testDir: "./test",
  testMatch: /.*\.spec\.ts$/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 30_000 },
  reporter: [["line"]],
  outputDir: process.env.SHELL_PW_OUT ?? join(tmpdir(), "prometheos-desktop-playwright"),
});
