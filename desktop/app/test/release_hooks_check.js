// Behavioural check of the release gate (decision 0072): runs a built app's own main.js and
// package.json, extracted from its app.asar, under the development Electron with
// PROMETHEOS_DESKTOP_TEST=1, and reports whether the shell's test hooks are exposed. A release
// build must answer false, a test build true.
// Usage: node desktop/app/test/release_hooks_check.js <unpacked app folder> <new work folder>
// A probe preloaded through NODE_OPTIONS (the development Electron allows it; a built app's
// fuse does not) reads the hooks once the app is ready, writes its answer and exits the app.
// The app gets a fresh home inside the work folder and no core, so its sidecar fails to start,
// which is expected. The work folder is left for the caller to remove.
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const asar = require("@electron/asar");

// Preloaded modules run before Electron registers its own module, so the probe waits a turn.
const PROBE = `
setImmediate(() => {
  const { app } = require("electron");
  app.whenReady().then(() => setTimeout(() => {
    const answer = { test_hooks_exposed: typeof globalThis.prometheosShell !== "undefined" };
    require("node:fs").writeFileSync(process.env.PROMETHEOS_PROBE_OUT, JSON.stringify(answer));
    app.exit(0);
  }, 2000));
});
`;

function main(folder, work) {
  fs.mkdirSync(work, { recursive: false });
  const appDir = path.join(work, "app");
  asar.extractAll(path.join(folder, "resources", "app.asar"), appDir);
  const probe = path.join(work, "probe.js");
  const answer = path.join(work, "answer.json");
  fs.writeFileSync(probe, PROBE);
  const done = spawnSync(require("electron"), [appDir], {
    env: {
      ...process.env,
      NODE_OPTIONS: `--require ${JSON.stringify(probe)}`,
      PROMETHEOS_DESKTOP_TEST: "1",
      PROMETHEOS_HOME: path.join(work, "home"),
      PROMETHEOS_PYTHON: path.join(work, "no-core", "python.exe"),
      PROMETHEOS_PROBE_OUT: answer,
    },
    stdio: "ignore",
    timeout: 60_000,
  });
  if (!fs.existsSync(answer)) {
    throw new Error(`the probe wrote no answer (status ${done.status}, ${done.error || "no error"})`);
  }
  const stamped = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf-8"))
    .prometheosRelease === true;
  const report = { release_build: stamped, ...JSON.parse(fs.readFileSync(answer, "utf-8")) };
  process.stdout.write(JSON.stringify(report) + "\n");
}

try {
  main(process.argv[2], process.argv[3]);
} catch (err) {
  process.stderr.write(`${err.stack || err}\n`);
  process.exit(1);
}
