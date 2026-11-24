// C19-WALK, the desktop leg (CP19.4; decision 0055). The owner's steps on the BUILT desktop app
// (dist/win-unpacked/PrometheOS.exe, with its bundled core) against store A, driven by
// Playwright's Electron driver. tools/walk_cp19.py runs the agent's steps over the real MCP
// server between them and checks the store; the two hand over through signal files.
//
//   W1.1 start the app hidden and open it from its tray icon; it pairs through the hub's route
//   W2.1 quick capture through the global hotkey: a note is typed and saved in the window
//   (agent: W3.1 suggests a work item, W3.2 asks to acknowledge its event -> approval_required,
//    W3.3 a boundary control: resuming before the owner decides is refused)
//   W4.1 the app announces the approval request as a notification
//   W4.2 the owner opens the request in the window, reviews its plan and grants it
//   (agent: W5.1 resumes the granted request)
//   W6.1 quit and restart: the store, the window's bounds and the announced events are kept
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";

type Info = { home: string; project_id: string; exe: string; signals: string; shots: string; steps: string; press_hotkey: string; python: string };
const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;

function record(step: Record<string, unknown>): void {
  appendFileSync(info.steps, JSON.stringify(step) + "\n");
}

function signal(name: string, data: unknown = {}): void {
  // Written whole, then renamed: the harness never reads half a signal (review s3d-a F7).
  const temp = join(info.signals, `${name}.tmp`);
  writeFileSync(temp, JSON.stringify(data));
  renameSync(temp, join(info.signals, `${name}.json`));
}

async function waitFor(name: string, seconds = 300): Promise<any> {
  const path = join(info.signals, `${name}.json`);
  const until = Date.now() + seconds * 1000;
  while (Date.now() < until) {
    if (existsSync(path)) return JSON.parse(readFileSync(path, "utf-8"));
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`the harness did not send ${name} in time`);
}

function env(hooks = true): Record<string, string> {
  const base = Object.fromEntries(Object.entries(process.env).filter(([, v]) => v !== undefined)) as Record<string, string>;
  delete base.PROMETHEOS_PYTHON; // the built app's own core, nothing from this machine
  delete base.PROMETHEOS_DESKTOP_TEST;
  const test: Record<string, string> = hooks ? { PROMETHEOS_DESKTOP_TEST: "1" } : {};
  return { ...base, PROMETHEOS_HOME: info.home, PROMETHEOS_DESKTOP_POLL_MS: "1000", ...test };
}

async function launch(hooks = true): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({ executablePath: info.exe, args: ["--project", info.project_id, "--hidden"], env: env(hooks) });
  const page = await app.firstWindow();
  return { app, page };
}

async function shell(app: ElectronApplication): Promise<any> {
  return app.evaluate(({ BrowserWindow }) => {
    const s = (globalThis as any).prometheosShell.state;
    const win = BrowserWindow.getAllWindows()[0];
    return { status: s.status, url: s.url, notifications: [...s.notifications], hotkey: { ...s.hotkey },
             trayReady: !!s.trayReady, visible: win ? win.isVisible() : false,
             bounds: win ? win.getBounds() : null };
  });
}

async function shot(page: Page, name: string): Promise<void> {
  mkdirSync(info.shots, { recursive: true });
  await page.screenshot({ path: join(info.shots, `${name}.png`), fullPage: true });
}

test("C19-WALK desktop leg", async () => {
  // Without the test switch the built app has no test hooks: none in its main process (where
  // the driver evaluates), and none of Node's or the shell's globals in the hub page.
  const bare = await launch(false);
  const hooksWithoutSwitch = await bare.app.evaluate(() => typeof (globalThis as any).prometheosShell);
  await expect.poll(() => bare.page.url(), { timeout: 90_000 }).toMatch(/^http:\/\/127\.0\.0\.1:/);
  const pageGlobals = await bare.page.evaluate(() =>
    ["prometheosShell", "require", "process", "electron"].map((n) => typeof (window as any)[n]).join(" "));
  await bare.app.close();

  let { app, page } = await launch();

  // W1.1: hidden at start, opened from the tray icon (its click, emitted in the app's own
  // process: an OS tray click cannot be scripted); the window pairs through the hub's route.
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  const hidden = await shell(app);
  await app.evaluate(() => (globalThis as any).prometheosShell.clickTray());
  await expect.poll(async () => (await shell(app)).visible).toBe(true);
  const session = await page.evaluate(() => fetch("/api/v1/session").then((r) => r.json()));
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  // A boundary control (review s3d-a F2): a loopback server on another port counts every
  // request. A load the main process starts toward it (no page CSP or navigation handler in the
  // way) and a navigation the page starts must both be blocked by the shell, with no hit.
  let probeHits = 0;
  const probe = createServer((_req, res) => { probeHits += 1; res.end("probe"); });
  await new Promise<void>((done) => probe.listen(0, "127.0.0.1", () => done()));
  const probeUrl = `http://127.0.0.1:${(probe.address() as { port: number }).port}/`;
  const probeMainLoad = await app.evaluate(async ({ BrowserWindow }, url) => {
    try {
      await BrowserWindow.getAllWindows()[0].webContents.loadURL(url);
      return "loaded";
    } catch (err) {
      return String((err as Error).message);
    }
  }, probeUrl);
  await app.evaluate(({ BrowserWindow }, url) => BrowserWindow.getAllWindows()[0].webContents.loadURL(url), hidden.url);
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  await page.evaluate((url) => { window.location.href = url; }, probeUrl);
  await page.waitForTimeout(1500);
  const stayed = new URL(page.url()).origin === new URL(hidden.url).origin;
  probe.close();
  await shot(page, "W1.1-opened-from-tray");
  record({ id: "W1.1", hidden_at_start: !hidden.visible, tray: hidden.trayReady,
           visible_after_tray_click: true, paired: session.paired, url: hidden.url,
           probe_main_load: probeMainLoad, probe_navigation_stayed: stayed, probe_hits: probeHits,
           hooks_without_switch: hooksWithoutSwitch, page_globals_without_switch: pageGlobals,
           hooks_with_switch: await app.evaluate(() => typeof (globalThis as any).prometheosShell?.clickTray) });
  signal("owner-opened");
  await waitFor("opened-checked");

  // W2.1: the global hotkey (pressed with SendInput, as a person would) opens quick capture.
  spawnSync(info.python, [info.press_hotkey]);
  await expect.poll(() => page.url()).toContain("/capture");
  await page.getByLabel("Title", { exact: true }).fill("Walk note from the hotkey");
  await page.getByLabel("Note", { exact: true }).fill("Captured in the desktop window (C19-WALK).");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("capture-saved")).toContainText("Walk note from the hotkey");
  await shot(page, "W2.1-captured");
  record({ id: "W2.1", hotkey_fired: (await shell(app)).hotkey.fired, saved: true });
  signal("owner-captured");

  // The agent suggests a work item and asks to acknowledge its event (W3.1, W3.2).
  const asked = await waitFor("agent-requested");

  // W4.1: the request reaches the owner as a notification.
  await expect.poll(async () => (await shell(app)).notifications.join("\n"), { timeout: 60_000 })
    .toContain("An approval was requested for event_acknowledge");
  record({ id: "W4.1", notifications: (await shell(app)).notifications });

  // W4.2: the owner reviews the request's plan in the window and grants it.
  await page.goto(new URL("approvals", (await shell(app)).url).toString());
  const card = page.locator(`[data-testid="approval-pending"][data-approval-id="${asked.approval_id}"]`);
  await expect(card).toBeVisible();
  await expect(card.getByTestId("approval-grant")).toBeDisabled();
  await card.getByTestId("approval-review").click();
  await expect(card.getByTestId("approval-plan")).toBeVisible();
  // The digest of exactly the plan the agent asked for is on the card before the grant.
  const planDigestShown = (await card.textContent() ?? "").includes(asked.plan_sha256);
  await shot(page, "W4.2-reviewing");
  await card.getByTestId("approval-grant").click();
  await expect(card).toHaveCount(0);
  await expect(page.locator(`[data-testid="approval-granted"][data-approval-id="${asked.approval_id}"]`)).toBeVisible();
  await shot(page, "W4.2-granted");
  record({ id: "W4.2", granted_in_window: true, plan_sha256: asked.plan_sha256, plan_digest_shown: planDigestShown });
  signal("owner-granted");

  // The agent resumes (W5.1).
  await waitFor("agent-resumed");

  // W6.1: quit (graceful sidecar stop), restart; the app's own state is kept.
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setBounds({ x: 140, y: 100, width: 1040, height: 720 }));
  const before = await shell(app);
  signal("owner-quitting");
  await waitFor("store-before-restart");
  await app.close();
  const stop = JSON.parse(readFileSync(join(info.home, "desktop", "settings.json"), "utf-8")).last_stop;
  ({ app, page } = await launch());
  await expect.poll(async () => (await shell(app)).status, { timeout: 90_000 }).toBe("running");
  await page.waitForTimeout(2500); // two polls
  const after = await shell(app);
  await app.evaluate(() => (globalThis as any).prometheosShell.clickTray());
  await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
  await shot(page, "W6.1-restarted");
  record({ id: "W6.1", last_stop: stop, bounds_kept: after.bounds?.width === 1040 && after.bounds?.height === 720,
           notifications_before_quit: before.notifications, notifications_after_restart: after.notifications,
           url: after.url });
  signal("owner-restarted");
  await waitFor("agent-reread");
  await app.close();
});
