// CP19.2 (C19-SHELL, decision 0054): the real desktop app on a fresh synthetic home. The window
// shows the paired hub over loopback, the core runs as a sidecar the app starts and stops
// gracefully, the tray and a notification for a research event appear, the global hotkey opens
// quick capture, a second launch hands over, nothing outside the hub's origin is reached, and
// quit and restart keep the app's own state (window bounds, events already announced).
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const APP = resolve(__dirname, "..");
const REPO = resolve(APP, "..", "..");
const PY = join(REPO, ".venv", "Scripts", "python.exe");
// The installed app (decision 0054): its own PrometheOS.exe with the bundled core, no Python
// from this machine. Without it, the development Electron runs this folder's main.js.
const INSTALLED = process.env.PROMETHEOS_DESKTOP_EXE;
const ELECTRON = INSTALLED ?? join(APP, "node_modules", "electron", "dist", "electron.exe");
const APP_ARGS = INSTALLED ? [] : [APP];
// The development app runs the built core when there is one (build_core.py): the same
// interpreter the installer ships, so isolation is tested where it matters. A venv Python
// would never load a user site anyway (review s3c2-a R2-F1).
const BUILT_CORE = join(APP, "build", "core", "python.exe");
const DEV_CORE = existsSync(BUILT_CORE) ? BUILT_CORE : PY;

type Fixture = { home: string; project_id: string; event_id: string };

function seed(): Fixture {
  const dir = mkdtempSync(join(tmpdir(), "pdesk-"));
  return JSON.parse(execFileSync(PY, [join(APP, "test", "seed.py"), join(dir, "home")], { encoding: "utf-8" }));
}

function seedMore(fx: Fixture, ...args: string[]): any {
  return JSON.parse(execFileSync(PY, [join(APP, "test", "seed.py"), args[0], fx.home, fx.project_id, ...args.slice(1)],
                                 { encoding: "utf-8" }));
}

function env(fx: Fixture, hooks = true): Record<string, string> {
  const base = Object.fromEntries(Object.entries(process.env).filter(([, v]) => v !== undefined)) as Record<string, string>;
  const core = INSTALLED ? {} : { PROMETHEOS_PYTHON: DEV_CORE };
  delete base.PROMETHEOS_PYTHON;
  // A package named prometheos that fails on import, first on PYTHONPATH: only an isolated
  // core (python -I) starts at all (review s3c2-a F1).
  const poison = join(fx.home, "..", "poison");
  mkdirSync(join(poison, "prometheos"), { recursive: true });
  writeFileSync(join(poison, "prometheos", "__init__.py"), "raise ImportError('not the bundled core')\n");
  base.PYTHONPATH = poison;
  // The same poison as this user's own Python site (%APPDATA%\Python\Python314\site-packages), the
  // path that leaked in round 1: -E alone would ignore PYTHONPATH but still load it (s3c2-a R2-F1).
  const appdata = join(fx.home, "..", "appdata");
  const userSite = join(appdata, "Python", "Python314", "site-packages", "prometheos");
  mkdirSync(userSite, { recursive: true });
  writeFileSync(join(userSite, "__init__.py"), "raise ImportError('not the bundled core')\n");
  base.APPDATA = appdata;
  delete base.PROMETHEOS_DESKTOP_TEST;
  const test: Record<string, string> = hooks ? { PROMETHEOS_DESKTOP_TEST: "1" } : {};
  return { ...base, ...core, PROMETHEOS_HOME: fx.home, PROMETHEOS_DESKTOP_POLL_MS: "1000", ...test };
}

async function launch(fx: Fixture, hooks = true): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({ executablePath: ELECTRON, args: [...APP_ARGS, "--project", fx.project_id], env: env(fx, hooks) });
  const page = await app.firstWindow();
  return { app, page };
}

async function shell(app: ElectronApplication): Promise<any> {
  return app.evaluate(() => {
    const s = (globalThis as any).prometheosShell.state;
    return { status: s.status, url: s.url, notifications: [...s.notifications], hotkey: { ...s.hotkey },
             sidecarPid: s.sidecar ? s.sidecar.pid : null, trayReady: !!s.trayReady,
             maxPollsInFlight: s.maxPollsInFlight || 0 };
  });
}

function settings(fx: Fixture): any {
  return JSON.parse(readFileSync(join(fx.home, "desktop", "settings.json"), "utf-8"));
}

/** The settings file, or nothing yet (the first save happens during the run). */
function settingsOr(fx: Fixture): any {
  try {
    return settings(fx);
  } catch {
    return {};
  }
}

function alive(pid: number): boolean {
  const out = spawnSync("tasklist", ["/FI", `PID eq ${pid}`, "/NH"], { encoding: "utf-8" }).stdout;
  return out.includes(String(pid));
}

function portOpen(port: number): Promise<boolean> {
  return new Promise((done) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    socket.once("connect", () => { socket.destroy(); done(true); });
    socket.once("error", () => done(false));
  });
}

test("the desktop app runs the hub as its sidecar and keeps its state across a restart", async () => {
  const fx = seed();
  let { app, page } = await launch(fx);

  // The window shows the hub over loopback, already paired through the hub's own route.
  await expect.poll(async () => (await shell(app)).status).toBe("running");
  const first = await shell(app);
  expect(new URL(first.url).hostname).toBe("127.0.0.1");
  expect(new URL(page.url()).origin).toBe(new URL(first.url).origin);
  const session = await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()));
  expect(session.paired).toBe(true);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  expect(first.trayReady).toBe(true);

  // The sidecar is the bundled core when installed, this repository's Python otherwise
  // (review s3c2-a F8).
  const sidecarExe = spawnSync("powershell", ["-NoProfile", "-Command",
    `(Get-Process -Id ${first.sidecarPid}).Path`], { encoding: "utf-8" }).stdout.trim();
  if (INSTALLED) expect(sidecarExe.toLowerCase()).toBe(join(INSTALLED, "..", "resources", "core", "python.exe").toLowerCase());
  else expect(sidecarExe.toLowerCase()).toBe(DEV_CORE.toLowerCase());

  // A notification for the research event the agent's suggestion raised.
  await expect.poll(async () => (await shell(app)).notifications)
    .toEqual([expect.stringContaining("A work item was added")]);
  expect(Object.keys(settings(fx).projects[fx.project_id].seen)).toContain(fx.event_id);

  // More than a queue page of open events: every new one is still considered (one summary
  // line), and an event stamped behind the shell's cursor, as a detector pass that waited for
  // the write lock stamps it, is announced exactly once (review s3c1-a F3, R2-F1).
  const poll = () => app.evaluate(() => (globalThis as any).prometheosShell.poll());
  const before = (await shell(app)).notifications.length;
  const bulk = seedMore(fx, "bulk", "120").event_ids as string[];
  expect(bulk).toHaveLength(120);
  await poll();
  await expect.poll(() => Object.keys(settings(fx).projects[fx.project_id].seen))
    .toEqual(expect.arrayContaining(bulk));
  // However the background polls split them, the 120 events were announced once in all.
  const notes: string[] = (await shell(app)).notifications.slice(before);
  const announced = notes.filter((n) => n.startsWith("Suggestion ")).length
    + notes.map((n) => /^(\d+) more research events/.exec(n)).reduce((sum, m) => sum + (m ? Number(m[1]) : 0), 0);
  expect(announced).toBe(120);
  const late = seedMore(fx, "late", "120").deadline_events as string[];
  expect(late.length).toBeGreaterThan(0);
  const deadlineNotes = async () => (await shell(app)).notifications
    .filter((n: string) => n.startsWith("Submit the abstract: A work item's deadline")).length;
  await Promise.all([poll(), poll()]); // two at once still announce once (review R3-F3)
  expect(await deadlineNotes()).toBe(late.length);
  expect((await shell(app)).maxPollsInFlight).toBe(1); // they never overlapped (s3c2-a F7)
  await poll();
  expect(await deadlineNotes()).toBe(late.length); // never twice
  expect(Object.keys(settings(fx).projects[fx.project_id].seen)).toEqual(expect.arrayContaining(late));

  // The shell's own guard: a load the main process starts toward another loopback port (no
  // page CSP or navigation handler in the way) never reaches it (review s3c1-a F1).
  let probeHits = 0;
  const probe = createServer((_req, res) => { probeHits += 1; res.end("probe"); });
  await new Promise<void>((done) => probe.listen(0, "127.0.0.1", () => done()));
  const probeUrl = `http://127.0.0.1:${(probe.address() as { port: number }).port}/`;
  const loaded = await app.evaluate(async ({ BrowserWindow }, url) => {
    try {
      await BrowserWindow.getAllWindows()[0].webContents.loadURL(url);
      return "loaded";
    } catch (err) {
      return String((err as Error).message);
    }
  }, probeUrl);
  expect(loaded).toContain("ERR_BLOCKED_BY_CLIENT");
  expect(probeHits).toBe(0);
  probe.close();
  // Back to the hub from the main process: page.goto can race the blocked load's error page.
  await app.evaluate(({ BrowserWindow }, url) => BrowserWindow.getAllWindows()[0].webContents.loadURL(url), first.url);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();

  // A poll that a page load interrupts settles (its page request is bounded) and polling goes
  // on: before decision 0055 the interrupted request never settled, the single-flight poll
  // stayed held and no event was announced again (found by C19-WALK).
  const interrupted = await app.evaluate(async ({ BrowserWindow }, [away, hub]) => {
    const contents = BrowserWindow.getAllWindows()[0].webContents;
    const poll = (globalThis as any).prometheosShell.poll();
    await contents.loadURL(away).catch(() => undefined); // blocked: the window's page is replaced
    await contents.loadURL(hub);
    return Promise.race([poll.then(() => "settled", () => "settled"),
                         new Promise((done) => setTimeout(() => done("still held"), 40_000))]); // 15 s bound + margin
  }, [probeUrl, first.url]);
  expect(interrupted).toBe("settled");
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  const afterInterrupt = seedMore(fx, "bulk", "1").event_ids as string[];
  await expect.poll(async () => Object.keys(settings(fx).projects[fx.project_id].seen), { timeout: 60_000 })
    .toEqual(expect.arrayContaining(afterInterrupt));

  // An ended session (it lasts 8 hours) is paired again by the app, with a new one-use code
  // from its sidecar (review s3c1-a F2).
  await app.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows()[0].webContents.session.clearStorageData({ storages: ["cookies"] }));
  const ended = await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()));
  expect(ended.paired).toBe(false);
  await app.evaluate(() => (globalThis as any).prometheosShell.poll());
  await expect.poll(async () =>
    (await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()))).paired).toBe(true);

  // Nothing outside the hub's origin is reached or shown in the window.
  const blocked = await page.evaluate(() => fetch("http://192.0.2.1/").then(() => "reached", () => "blocked"));
  expect(blocked).toBe("blocked");
  await page.evaluate(() => { window.location.href = "http://192.0.2.1/elsewhere"; });
  await page.waitForTimeout(1000);
  expect(new URL(page.url()).origin).toBe(new URL(first.url).origin);

  // The global hotkey, pressed as a person would, opens quick capture.
  expect(first.hotkey.registered).toBe(true);
  spawnSync(PY, [join(REPO, "desktop", "spike", "press_hotkey.py")]);
  await expect.poll(() => page.url()).toContain("/capture");
  expect((await shell(app)).hotkey.fired).toBe(1);

  // A second launch hands over to the running app and exits.
  const second = spawnSync(ELECTRON, [...APP_ARGS, "--project", fx.project_id], { env: env(fx), timeout: 60_000 });
  expect(second.status).toBe(0);
  expect((await shell(app)).sidecarPid).not.toBeNull();

  // Quit: the sidecar stops gracefully (end of input), its process and port are gone.
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setBounds({ x: 120, y: 90, width: 1000, height: 700 }));
  const sidecarPid = (await shell(app)).sidecarPid as number;
  const announcedBefore: string[] = Object.keys(settings(fx).projects[fx.project_id].seen);
  const port = Number(new URL(first.url).port);
  await app.close();
  await expect.poll(() => alive(sidecarPid)).toBe(false);
  expect(await portOpen(port)).toBe(false);
  const stopped = settings(fx).last_stop;
  expect(stopped).toMatchObject({ graceful: true, code: 0 });

  // Restart: the same bounds, the project as it was, and the event is not announced again.
  ({ app, page } = await launch(fx));
  await expect.poll(async () => (await shell(app)).status).toBe("running");
  const bounds = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getBounds());
  // Decision 0071: at a fractional display scale (125% on the owner's laptop) Windows cannot
  // place a window at every logical size, and Electron lands a few pixels off (700 -> 702 ->
  // 707). Exact at an integer scale; within 8 logical pixels otherwise.
  const scale = await app.evaluate(({ screen }) => screen.getPrimaryDisplay().scaleFactor);
  if (Number.isInteger(scale)) {
    expect(bounds).toMatchObject({ width: 1000, height: 700 });
  } else {
    expect(Math.abs(bounds.width - 1000), `width ${bounds.width} at scale ${scale}`).toBeLessThanOrEqual(8);
    expect(Math.abs(bounds.height - 700), `height ${bounds.height} at scale ${scale}`).toBeLessThanOrEqual(8);
  }
  await page.goto(new URL("work", (await shell(app)).url).toString());
  // Decision 0078: the bulk suggestions seeded above fold the unlabelled ones into a closed group.
  const unlabelled = page.locator("details").filter({ has: page.locator("summary", { hasText: "Without a label" }) });
  await unlabelled.locator("summary").click();
  await expect(unlabelled.getByRole("link", { name: "Tidy the figure folder" })).toBeVisible();
  await page.waitForTimeout(2500); // two more polls
  // Nothing announced before the quit comes back. The hub's own detector pass at its start
  // may record a genuinely new event (a nearer deadline threshold): that one is announced once.
  const after = new Set<string>(Object.keys(settings(fx).projects[fx.project_id].seen));
  for (const id of announcedBefore) expect(after.has(id)).toBe(true);
  const newSince = [...after].filter((id) => !announcedBefore.includes(id));
  expect((await shell(app)).notifications).toHaveLength(newSince.length);
  await app.close();
  // The install check keeps the home to show that uninstalling leaves project data alone.
  if (!process.env.PROMETHEOS_DESKTOP_KEEP_HOME) rmSync(resolve(fx.home, ".."), { recursive: true, force: true }); // review s3c1-a F9
  if (process.env.PROMETHEOS_DESKTOP_KEEP_HOME) writeFileSync(process.env.PROMETHEOS_DESKTOP_KEEP_HOME, JSON.stringify(fx));
});

// Decision 0055: the shell's test hooks (its state, clickTray) exist only under the explicit
// test switch PROMETHEOS_DESKTOP_TEST=1. Without it the main process has none, and the hub page
// (sandboxed, context-isolated, no preload, no IPC) has none of Node's or the shell's globals.
test("without the test switch the app exposes no test hooks", async () => {
  const fx = seed();
  const { app, page } = await launch(fx, false);
  expect(await app.evaluate(() => typeof (globalThis as any).prometheosShell)).toBe("undefined");
  await expect.poll(() => page.url(), { timeout: 90_000 }).toMatch(/^http:\/\/127\.0\.0\.1:/);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  const globals = await page.evaluate(() =>
    ["prometheosShell", "require", "process", "electron"].map((n) => typeof (window as any)[n]));
  expect(globals).toEqual(["undefined", "undefined", "undefined", "undefined"]);
  await app.close();
  rmSync(resolve(fx.home, ".."), { recursive: true, force: true });
});

// Review int8 CP19-F6 (decision 0056): when the core dies, the tray says so and offers to restart
// it; restarting from the tray brings a new core and a paired window back.
test("the tray shows a stopped hub and restarts it", async () => {
  const fx = seed();
  const { app, page } = await launch(fx);
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  const menu = () => app.evaluate(() => (globalThis as any).prometheosShell.menu().items
    .map((i: any) => ({ label: i.label, enabled: i.enabled })));
  const restart = async () => (await menu()).find((i: any) => i.label === "Restart the hub");
  const tooltip = () => app.evaluate(() => (globalThis as any).prometheosShell.state.trayTooltip);
  expect((await restart()).enabled).toBe(false); // nothing to restart while the core runs
  const first = await shell(app);
  spawnSync("taskkill", ["/PID", String(first.sidecarPid), "/F"]);
  await expect.poll(async () => (await shell(app)).status, { timeout: 30_000 }).toMatch(/^stopped \(exit \d+\)$/);
  expect(await tooltip()).toMatch(/^PrometheOS: stopped \(exit \d+\)$/);
  expect((await restart()).enabled).toBe(true);
  expect((await shell(app)).sidecarPid).toBeNull();
  // The owner restarts the hub from the tray menu.
  await app.evaluate(() => (globalThis as any).prometheosShell.menu().items
    .find((i: any) => i.label === "Restart the hub").click());
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  const second = await shell(app);
  expect(second.sidecarPid).not.toBeNull();
  expect(second.sidecarPid).not.toBe(first.sidecarPid);
  expect(await tooltip()).toBe("PrometheOS: running");
  expect((await restart()).enabled).toBe(false);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  expect((await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()))).paired).toBe(true);
  await app.close();
  rmSync(resolve(fx.home, ".."), { recursive: true, force: true });
});

// Decision 0077: a restart keeps the saved window size even where Windows lands a restored window
// a few pixels off (dogfood P1, 125% scale), and the window paints the hub's theme colour from
// <meta name="theme-color">, remembered for the next start (no white flash before a dark hub).
test("the window keeps its saved bounds across restarts and follows the hub's theme", async () => {
  const fx = seed();
  let { app, page } = await launch(fx);
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  // Dark by default: the window and its title bar follow the hub's colour.
  await expect.poll(() => settingsOr(fx).theme_color).toBe("#0e1116");
  expect(await app.evaluate(({ nativeTheme }) => nativeTheme.themeSource)).toBe("dark");
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setBounds({ x: 140, y: 100, width: 1000, height: 700 }));
  await app.close();
  const saved = settings(fx).bounds;
  expect(Math.abs(saved.width - 1000)).toBeLessThanOrEqual(8);

  // Two restarts without touching the window: the saved bounds do not move.
  for (let restart = 0; restart < 2; restart++) {
    ({ app, page } = await launch(fx));
    await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
    await expect(page.getByTestId("theme-toggle")).toBeVisible();
    await page.getByTestId("theme-toggle").click();
    if (restart === 0) {
      // The owner switches the hub to light: the window follows and remembers it.
      await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "light");
      await expect.poll(() => settingsOr(fx).theme_color).toBe("#f6f7f9");
      expect(await app.evaluate(({ nativeTheme }) => nativeTheme.themeSource)).toBe("light");
    } else {
      // "Follow Windows": the shell leaves the page's colour scheme to Windows (themeSource
      // "system"), so the hub follows Windows rather than the shell's last colour.
      await expect(page.getByTestId("theme-toggle")).toHaveAttribute("data-theme-choice", "system");
      await expect.poll(() => settingsOr(fx).theme_follows_system).toBe(true);
      expect(await app.evaluate(({ nativeTheme }) => nativeTheme.themeSource)).toBe("system");
    }
    // A drift like Windows' at 125% (a few pixels) is not saved over the owner's size.
    await app.evaluate(({ BrowserWindow }) => {
      const w = BrowserWindow.getAllWindows()[0];
      const b = w.getBounds();
      w.setBounds({ ...b, width: b.width + 3, height: b.height + 3 });
    });
    await app.close();
    expect(settings(fx).bounds).toEqual(saved);
  }

  // The next start keeps following Windows and paints the matching colour before the page loads.
  ({ app, page } = await launch(fx));
  // A real resize (more than the drift) is saved.
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  await app.evaluate(({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    const b = w.getBounds();
    w.setBounds({ ...b, width: b.width + 40, height: b.height + 30 });
  });
  await app.close();
  expect(Math.abs(settings(fx).bounds.width - (saved.width + 40))).toBeLessThanOrEqual(8);
  ({ app, page } = await launch(fx));
  expect(await app.evaluate(({ nativeTheme }) => nativeTheme.themeSource)).toBe("system");
  const dark = await app.evaluate(({ nativeTheme }) => nativeTheme.shouldUseDarkColors);
  expect((await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getBackgroundColor())).toLowerCase())
    .toBe(dark ? "#0e1116" : "#f6f7f9");
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  await app.close();
  rmSync(resolve(fx.home, ".."), { recursive: true, force: true });
});
