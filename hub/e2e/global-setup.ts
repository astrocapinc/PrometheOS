// Start the real hub on a disposable synthetic project (tools/hub_fixture.py in this
// repository) with --static pointing at this folder's dist build, wait until it answers,
// and return the teardown.
import { spawn, spawnSync } from "node:child_process";
import { createWriteStream, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(HERE, "..");
const REPO = resolve(process.env.PROMETHEOS_REPO ?? join(ROOT, ".."));
const STATE = join(HERE, ".state");
const TOKENS = Number(process.env.HUB_TOKENS ?? "180");

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function killTree(pid: number | undefined) {
  if (!pid) return;
  if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"]);
  else {
    try {
      process.kill(-pid, "SIGTERM");
    } catch {
      /* already gone */
    }
  }
}

export default async function globalSetup() {
  // By default the hub serves the assets shipped in the Python package
  // (tools/build_hub.py copies dist/ there); HUB_STATIC=dist serves this folder's build.
  const dist = join(ROOT, "dist");
  const packaged = join(REPO, "src", "prometheos", "hub", "static");
  const useDist = process.env.HUB_STATIC === "dist";
  if (!existsSync(join(useDist ? dist : packaged, "index.html"))) {
    throw new Error(useDist ? "Build first: npm run build" : "Run: uv run python tools/build_hub.py");
  }
  if (!existsSync(join(REPO, "tools", "hub_fixture.py"))) {
    throw new Error(`repository not found at ${REPO}; set PROMETHEOS_REPO`);
  }
  rmSync(STATE, { recursive: true, force: true });
  mkdirSync(STATE, { recursive: true });
  const infoPath = join(STATE, "info.json");
  const log = createWriteStream(join(STATE, "hub.log"));
  const child = spawn(
    "uv",
    [
      "run",
      "--offline",
      "--no-sync",
      "python",
      "tools/hub_fixture.py",
      "--unavailable-models",
      "--info",
      infoPath,
      ...(useDist ? ["--static", dist] : []),
      "--tokens",
      String(TOKENS),
    ],
    {
      cwd: REPO,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      detached: process.platform !== "win32",
    },
  );
  child.stdout?.pipe(log);
  child.stderr?.pipe(log);
  let exited: number | null = null;
  child.on("exit", (code) => {
    exited = code ?? -1;
  });
  let workDir: string | null = null;
  const teardown = async () => {
    killTree(child.pid);
    // The fixture's disposable synthetic project lives in a temp folder it created.
    if (workDir && /prometheos-hub-fixture-/.test(workDir)) {
      for (let i = 0; i < 10; i += 1) {
        try {
          rmSync(workDir, { recursive: true, force: true });
          break;
        } catch {
          await sleep(300); // files may still be held for a moment after the kill
        }
      }
    }
  };

  const deadline = Date.now() + 180_000;
  while (!existsSync(infoPath)) {
    if (exited !== null) throw new Error(`hub_fixture exited early (${exited}); see e2e/.state/hub.log`);
    if (Date.now() > deadline) {
      await teardown();
      throw new Error("hub_fixture did not write its info file in time");
    }
    await sleep(250);
  }
  await sleep(200);
  const info = JSON.parse(readFileSync(infoPath, "utf-8")) as Record<string, unknown> & {
    url: string;
    work?: string;
  };
  workDir = info.work ?? null;
  // The info file is written just before uvicorn starts serving: wait for an answer.
  for (;;) {
    try {
      const reply = await fetch(new URL("/api/v1/session", info.url));
      if (reply.ok) break;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) {
      await teardown();
      throw new Error("hub did not answer in time");
    }
    await sleep(200);
  }
  const seed = (info.seed ?? {}) as Record<string, unknown>;
  writeFileSync(join(STATE, "hub.json"), JSON.stringify({ ...info, seed, hub_pid: child.pid }, null, 2));
  writeFileSync(join(STATE, "token-counter.txt"), "0");
  console.log(`[global-setup] real hub at ${info.url} (repo=${REPO})`);
  return teardown;
}
