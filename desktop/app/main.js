// PrometheOS desktop application (CP19.2; decisions 0047, 0050 and 0054).
//
// A window on the loopback hub, with the Python core as a sidecar this app starts and stops
// (`prometheos hub serve --sidecar`). It adds:
// - a tray icon showing the hub's state;
// - system notifications for new research events (CP18);
// - a global hotkey that opens quick capture;
// - one instance per user.
// It adds no authority:
// - the window pairs through the hub's own pairing route with the one-use code that the
//   sidecar hands to this process only;
// - every request leaves for the hub's loopback origin only;
// - the hub's pairing, CSRF and loopback rules are unchanged.
// Window and tray settings live in the per-user PrometheOS home, never in a project store.
"use strict";

const { app, BrowserWindow, Menu, Notification, Tray, globalShortcut, nativeImage, nativeTheme, shell } =
  require("electron");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const APP_ID = "org.prometheos.desktop";
const DEFAULT_HOTKEY = "Control+Alt+Shift+K";
const POLL_MS = Number(process.env.PROMETHEOS_DESKTOP_POLL_MS || 60_000);
const STOP_GRACE_MS = 15_000;
const MAX_NOTES_PER_POLL = 3;
const MAX_EVENT_PAGES = 10; // 2000 events per poll at most
const PAGE_FETCH_TIMEOUT_MS = 15_000; // one request through the hub page (decision 0055)
// Events are not committed in timestamp order: a detector pass stamps its events before it
// waits for the write lock, so one can land behind a cursor already read past. Each poll
// re-reads this far behind its cursor; the seen set removes what was already announced
// (review s3c1-a R2-F1).
const CURSOR_LOOKBACK_MS = 30 * 60 * 1000;
// A release build (build_app.py --release, decision 0072) stamps prometheosRelease into the
// packaged package.json: it never exposes the test hooks, whatever the environment says.
const RELEASE_BUILD = require("./package.json").prometheosRelease === true;
const TEST_HOOKS = !RELEASE_BUILD && process.env.PROMETHEOS_DESKTOP_TEST === "1";

// ----- the per-user home and settings -------------------------------------------------------

function prometheosHome() {
  return process.env.PROMETHEOS_HOME
    || path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "PrometheOS-v1");
}

const DESKTOP_DIR = path.join(prometheosHome(), "desktop");
const SETTINGS = path.join(DESKTOP_DIR, "settings.json");

function loadSettings() {
  let text;
  try {
    text = fs.readFileSync(SETTINGS, "utf-8");
  } catch {
    return {}; // first start
  }
  try {
    return JSON.parse(text);
  } catch {
    // A damaged file is kept beside the new one for the owner, never silently overwritten.
    try {
      fs.renameSync(SETTINGS, `${SETTINGS}.damaged-${Date.now()}`);
    } catch {
      // left in place; the next save replaces it
    }
    return {};
  }
}

function saveSettings(settings) {
  // Written to a temporary file, flushed to disk, then renamed over the old one: a crash or a
  // power loss leaves either the old settings or the new ones (review s3c1-a F8).
  try {
    fs.mkdirSync(DESKTOP_DIR, { recursive: true });
    const temp = `${SETTINGS}.tmp`;
    const fd = fs.openSync(temp, "w");
    try {
      fs.writeSync(fd, JSON.stringify(settings, null, 2));
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(temp, SETTINGS);
  } catch (err) {
    state.stderrTail.push(`settings not saved: ${err.message}`);
  }
}

function option(name) {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? process.argv[at + 1] : undefined;
}

/** The Python that runs the core: an explicit choice, else the one bundled with the app. */
function corePython(settings) {
  return process.env.PROMETHEOS_PYTHON
    || settings.python
    || path.join(process.resourcesPath || "", "core", "python.exe");
}

// The Electron profile (caches, cookies) belongs to this app's per-user folder in the home, so
// one PrometheOS home is one instance and uninstalling the app leaves the home alone.
app.setPath("userData", path.join(DESKTOP_DIR, "electron"));
app.setAppUserModelId(APP_ID);

const state = {
  settings: loadSettings(),
  status: "starting",
  sidecar: null,
  url: null,
  pairingCode: null,
  waiters: [],
  stderrTail: [],
  quitting: false,
  notifications: [], // for tests and the tray: what was shown this run
  hotkey: { accelerator: null, registered: false, fired: 0 },
};
let win = null;
let tray = null;
let trayMenu = null; // the tray's current context menu (its "Restart the hub" follows the hub)

// ----- the sidecar ---------------------------------------------------------------------------

function nextPairing(timeoutMs = 60_000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("the hub sent no pairing code in time")), timeoutMs);
    state.waiters.push((code, error) => {
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(code);
    });
  });
}

function failWaiters(error) {
  for (const waiter of state.waiters.splice(0)) waiter(null, error);
}

function startSidecar(projectId) {
  const python = corePython(state.settings);
  // -I: isolated, so the bundled, locked core is what runs, never a package from this user's
  // own Python site or a PYTHONPATH (review s3c2-a F1); -X utf8 replaces PYTHONIOENCODING,
  // which -I ignores.
  const child = spawn(python, ["-I", "-X", "utf8", "-m", "prometheos", "hub", "serve", "--sidecar",
                               "--project", projectId], {
    env: { ...process.env, PROMETHEOS_HOME: prometheosHome() },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  state.sidecar = child;
  const lines = readline.createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return; // not one of the sidecar's lines
    }
    if (message.event === "pairing") {
      state.url = message.url;
      state.pairingCode = message.pairing_code;
      for (const waiter of state.waiters.splice(0)) waiter(message.pairing_code);
    }
  });
  // The hub writes warnings to stderr: always read it, or a full pipe would block the hub.
  child.stderr.on("data", (chunk) => {
    state.stderrTail.push(String(chunk));
    state.stderrTail = state.stderrTail.slice(-50);
  });
  // A Python that cannot start, or a hub that exits before its first line, fails the start at
  // once instead of after the pairing timeout (review s3c1-a F5).
  child.on("error", (err) => {
    state.sidecar = null;
    failWaiters(new Error(`cannot start the core (${python}): ${err.message}`));
    if (!state.quitting) setStatus(`the hub did not start: ${err.message}`);
  });
  child.stdin.on("error", () => {}); // a hub that already exited closes its end first
  child.on("exit", (code) => {
    state.sidecar = null;
    failWaiters(new Error(`the hub exited (code ${code}) ${state.stderrTail.slice(-3).join(" ").trim()}`));
    if (!state.quitting) setStatus(`stopped (exit ${code})`);
  });
  return nextPairing();
}

/** Graceful stop: end of input tells the hub to finish and close its store; a hub that does
 * not stop in time is ended with its whole process tree. */
function stopSidecar() {
  const child = state.sidecar;
  if (!child) return Promise.resolve({ graceful: true, code: null });
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
      resolve({ graceful: false, code: null });
    }, STOP_GRACE_MS);
    child.once("exit", (code) => {
      clearTimeout(timer);
      resolve({ graceful: true, code });
    });
    // end() alone leaves a Windows pipe handle open, so the hub would never see end of input.
    child.stdin.end();
    child.stdin.destroy();
  });
}

// ----- the window, held to the hub's origin -------------------------------------------------

function hubOrigin() {
  return state.url ? new URL(state.url).origin : null;
}

function sameOrigin(target) {
  try {
    return new URL(target).origin === hubOrigin();
  } catch {
    return false;
  }
}

function guardSession(session) {
  // Only the hub's own origin is reached; nothing else leaves this window (the loopback rule).
  session.webRequest.onBeforeRequest({ urls: ["<all_urls>"] }, (details, callback) => {
    const url = details.url;
    const allowed = url.startsWith("devtools:") || url.startsWith("data:") || url.startsWith("blob:")
      || (hubOrigin() !== null && sameOrigin(url));
    callback({ cancel: !allowed });
  });
  session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
}

// ----- the window's theme (decision 0077) ---------------------------------------------------
// The hub keeps <meta name="theme-color"> at its page background (dark by default; light or
// following Windows once the owner picks one). The window paints that colour before the page
// loads, its title bar follows it, and the last colour is remembered for the next start, so
// the window never flashes white before a dark hub.
const DEFAULT_THEME_COLOR = "#0e1116";
const LIGHT_THEME_COLOR = "#f6f7f9";

function themeColor(color) {
  return typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color) ? color.toLowerCase() : null;
}

function isDark(color) {
  const [r, g, b] = [1, 3, 5].map((at) => parseInt(color.slice(at, at + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5;
}

// themeSource is also what the hub page sees as prefers-color-scheme. When the owner chose
// "follow Windows" in the hub, forcing dark or light here would make the hub follow the shell
// instead of Windows (and stay there), so that choice keeps themeSource on "system". The hub
// marks that mode with its two "follow Windows" colours (hub/src/theme.ts).
const SYSTEM_THEME_COLORS = new Set(["#0e1117", "#f6f7f8"]);

function themeSourceFor(color, followsSystem) {
  if (followsSystem) return "system";
  return isDark(color) ? "dark" : "light";
}

/** The theme the owner last chose in the hub, as the shell remembers it; null before any. */
function rememberedTheme() {
  if (state.settings.theme_follows_system === true) return "system";
  const color = themeColor(state.settings.theme_color);
  if (!color) return null;
  return isDark(color) ? "dark" : "light";
}

/** A hub URL that carries the remembered theme. The sidecar takes a new port on each start, so
 * the hub's own storage (one per origin) does not keep the owner's choice across restarts; the
 * hub reads ?theme= before its first paint and removes it from the address. */
function withTheme(url) {
  const theme = rememberedTheme();
  if (!theme) return url;
  const target = new URL(url);
  target.searchParams.set("theme", theme);
  return target.toString();
}

// ----- the window's bounds (decisions 0071 and 0077) ----------------------------------------
// At a fractional display scale Windows lands a restored window a few pixels off the saved
// size (1000x700 comes back 1000x702, then 703 …). Saving what it landed at would grow the
// window on every restart (dogfood P1). The saved bounds are kept unless the window moved or
// changed size by more than that drift since it was last shown. Normal bounds: a maximised or
// minimised window keeps the size it returns to.
const BOUNDS_DRIFT = 8;
let shownBounds = null;

function boundsToSave(current) {
  const saved = state.settings.bounds;
  if (!saved || !shownBounds) return current;
  const drifted = ["x", "y", "width", "height"].every(
    (key) => Math.abs(current[key] - shownBounds[key]) <= BOUNDS_DRIFT,
  );
  return drifted ? saved : current;
}

function createWindow() {
  const bounds = state.settings.bounds || { width: 1200, height: 820 };
  const followsSystem = state.settings.theme_follows_system === true;
  nativeTheme.themeSource = themeSourceFor(
    themeColor(state.settings.theme_color) || DEFAULT_THEME_COLOR, followsSystem);
  const background = followsSystem
    ? (nativeTheme.shouldUseDarkColors ? DEFAULT_THEME_COLOR : LIGHT_THEME_COLOR)
    : themeColor(state.settings.theme_color) || DEFAULT_THEME_COLOR;
  win = new BrowserWindow({
    ...bounds,
    title: "PrometheOS",
    backgroundColor: background,
    show: false,
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  guardSession(win.webContents.session);
  win.webContents.on("will-navigate", (event, target) => {
    if (!sameOrigin(target)) {
      event.preventDefault();
      openOutside(target);
    }
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (sameOrigin(url)) win.loadURL(withTheme(url));
    else openOutside(url);
    return { action: "deny" };
  });
  win.webContents.on("did-change-theme-color", (_event, color) => {
    const chosen = themeColor(color);
    if (!chosen || win.isDestroyed()) return;
    win.setBackgroundColor(chosen);
    const followsSystem = SYSTEM_THEME_COLORS.has(chosen);
    nativeTheme.themeSource = themeSourceFor(chosen, followsSystem);
    if (state.settings.theme_color !== chosen || state.settings.theme_follows_system !== followsSystem) {
      state.settings.theme_color = chosen;
      state.settings.theme_follows_system = followsSystem;
      saveSettings(state.settings);
    }
  });
  // Each time the window is shown again from the tray, its bounds then are the reference.
  win.on("show", () => {
    shownBounds = win.getNormalBounds();
  });
  win.on("close", (event) => {
    state.settings.bounds = boundsToSave(win.getNormalBounds());
    saveSettings(state.settings);
    // The page's storage (the hub's theme choice) is written now, not left to a later flush
    // that a quit can cut short.
    win.webContents.session.flushStorageData();
    if (!state.quitting) {
      event.preventDefault(); // closing hides to the tray; Quit ends the app
      win.hide();
    }
  });
  win.once("ready-to-show", () => {
    shownBounds = win.getNormalBounds();
    if (!process.argv.includes("--hidden")) win.show();
  });
}

/** A link to another site opens in the owner's browser, never inside the app: only https. */
function openOutside(target) {
  try {
    if (new URL(target).protocol === "https:") shell.openExternal(target);
  } catch {
    // not a URL: ignored
  }
}

/** Run a request from inside the hub page, with the page's own session and origin. */
function pageFetch(pathname, init) {
  const script = `fetch(${JSON.stringify(pathname)}, ${JSON.stringify(init || {})})
    .then(async (r) => ({ status: r.status, body: await r.text() }))`;
  // A page load that interrupts the script leaves executeJavaScript's promise unsettled for good;
  // a poll waiting on it would hold the single flight and no event would be announced again
  // (found by C19-WALK, decision 0055). Bounded: the call fails and the next poll runs.
  let timer;
  const expired = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("the hub page did not answer in time")), PAGE_FETCH_TIMEOUT_MS);
  });
  return Promise.race([win.webContents.executeJavaScript(script, true), expired])
    .finally(() => clearTimeout(timer));
}

async function ensurePaired() {
  const session = await pageFetch("/api/v1/session");
  if (session.status === 200 && JSON.parse(session.body).paired) return true;
  for (let attempt = 0; attempt < 2; attempt++) {
    const code = state.pairingCode || await askForPairing();
    state.pairingCode = null; // a code is good for one use
    const paired = await pageFetch("/api/v1/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: code }),
    });
    if (paired.status === 200) {
      await win.loadURL(withTheme(state.url));
      return true;
    }
  }
  return false;
}

let pairing = null;

/** Pair again when the hub says the session ended (it lasts 8 hours); one attempt at a time. */
function repair() {
  if (!pairing) {
    pairing = ensurePaired()
      .then((paired) => setStatus(paired ? "running" : "running, not paired"))
      .finally(() => {
        pairing = null;
      });
  }
  return pairing;
}

function askForPairing() {
  const waiting = nextPairing();
  if (state.sidecar) state.sidecar.stdin.write("pair\n");
  return waiting;
}

// ----- tray, notifications and the hotkey ---------------------------------------------------

function setStatus(status) {
  state.status = status;
  if (!tray || tray.isDestroyed()) return;
  state.trayTooltip = `PrometheOS: ${status}`;
  tray.setToolTip(state.trayTooltip);
  trayMenu = Menu.buildFromTemplate([
    { label: "Open PrometheOS", click: showWindow },
    { label: "Quick capture", click: openCapture },
    { label: "Restart the hub", enabled: !state.sidecar, click: () => boot() },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]);
  tray.setContextMenu(trayMenu);
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function openCapture() {
  if (!state.url) return;
  showWindow();
  win.loadURL(withTheme(new URL("capture", state.url).toString()));
}

/** The shell's own record for the open project: its events cursor and the events it announced
 * (kept per project, so a second project never inherits the first one's; review R2-F2). */
function projectRecord() {
  const projects = (state.settings.projects ||= {});
  const record = (projects[state.settings.project_id] ||= { seen: {}, events_cursor: null });
  record.seen ||= {};
  return record;
}

function stamp(cursor) {
  const at = cursor ? Date.parse(cursor.split("|")[0]) : Number.NaN;
  return Number.isNaN(at) ? null : at;
}

function lookback(cursor) {
  const at = stamp(cursor);
  if (at === null) return null;
  const earlier = new Date(at - CURSOR_LOOKBACK_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
  // A well-formed event id that never exists: the hub starts such a cursor at the beginning of
  // its second (decision 0058), so the lookback covers that whole second as well.
  return `${earlier}|evt_00000000-0000-4000-8000-000000000000`;
}

let polling = null;

/** One poll at a time: the timer and a re-pair could otherwise read the same new events twice
 * before either saved them as seen. */
function pollEvents() {
  if (!polling) polling = pollOnce().finally(() => { polling = null; });
  return polling;
}

async function pollOnce() {
  // Counted inside the poll itself, so the shell's tests can see that polls never overlap.
  state.pollsInFlight = (state.pollsInFlight || 0) + 1;
  state.maxPollsInFlight = Math.max(state.maxPollsInFlight || 0, state.pollsInFlight);
  try {
    await pollBody();
  } finally {
    state.pollsInFlight -= 1;
  }
}

async function pollBody() {
  if (!state.url || !win || win.isDestroyed()) return;
  const record = projectRecord();
  // Seen ids are kept by event time, for as long as a poll can read them again (the lookback
  // window and a margin), not by count: a count could forget an event the window still holds
  // and announce it again (review s3c1-a R3-F1).
  const seen = { ...record.seen }; // committed only by a poll that finished (review s3c2-a F2)
  const fresh = [];
  // Open events, oldest first, from a little before where this app last read (see above). A
  // poll that stopped at its page limit continues where it stopped, so a burst of events can
  // never hold the cursor still.
  let cursor = record.resume_cursor || lookback(record.events_cursor);
  let newest = record.events_cursor;
  let resume = null;
  for (let page = 0; page < MAX_EVENT_PAGES; page++) {
    const query = `/api/v1/events?state=open&limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    let answer;
    try {
      answer = await pageFetch(query);
      if (answer.status === 401) {
        await repair(); // the session ended: pair again, read on the next poll
        return;
      }
    } catch {
      return; // the next poll tries again
    }
    if (answer.status !== 200) return;
    const listing = JSON.parse(answer.body);
    for (const event of listing.items) {
      if (!(event.event_id in seen)) {
        fresh.push(event);
        seen[event.event_id] = event.occurred_at;
      }
      const position = `${event.occurred_at}|${event.event_id}`;
      if (!newest || position > newest) newest = position;
    }
    if (!listing.next_cursor) break;
    cursor = listing.next_cursor;
    if (page === MAX_EVENT_PAGES - 1) resume = cursor;
  }
  record.seen = seen;
  record.resume_cursor = resume;
  record.events_cursor = newest;
  const horizon = stamp(newest);
  if (horizon !== null) {
    const keepFrom = horizon - 2 * CURSOR_LOOKBACK_MS;
    for (const [id, at] of Object.entries(seen)) {
      if (Date.parse(at) < keepFrom) delete seen[id];
    }
  }
  for (const event of fresh.slice(0, MAX_NOTES_PER_POLL)) {
    const subject = event.subject_title ? `${event.subject_title}: ` : "";
    notify(`${subject}${event.reason}`);
  }
  if (fresh.length > MAX_NOTES_PER_POLL) {
    notify(`${fresh.length - MAX_NOTES_PER_POLL} more research events are in Now.`);
  }
  // Remembered across restarts: an event is announced once, never again after a restart.
  saveSettings(state.settings);
}

function notify(body) {
  const note = new Notification({ title: "PrometheOS", body });
  note.on("click", () => {
    showWindow();
    if (state.url) win.loadURL(withTheme(state.url));
  });
  note.show();
  state.notifications.push(body);
}

function registerHotkey() {
  const accelerator = state.settings.hotkey || DEFAULT_HOTKEY;
  state.hotkey.accelerator = accelerator;
  state.hotkey.registered = globalShortcut.register(accelerator, () => {
    state.hotkey.fired += 1;
    openCapture();
  });
}

// ----- start and quit ------------------------------------------------------------------------

function chooseProject() {
  return option("project") || process.env.PROMETHEOS_PROJECT || state.settings.project_id;
}

async function boot() {
  const projectId = chooseProject();
  if (!projectId) {
    setStatus("no project chosen (start with --project, or set project_id in the settings)");
    return;
  }
  state.settings.project_id = projectId;
  saveSettings(state.settings);
  setStatus("starting the hub");
  try {
    await startSidecar(projectId);
  } catch (err) {
    setStatus(`the hub did not start: ${err.message}`);
    return;
  }
  try {
    await win.loadURL(withTheme(state.url));
    await repair();
  } catch (err) {
    setStatus(`the window could not open the hub: ${err.message}`);
    return;
  }
  await pollEvents();
}

// Exposed only to the shell's own tests (Playwright's Electron driver evaluates in this
// process): the state holds the live pairing code (review s3c1-a F7).
if (TEST_HOOKS) {
  // clickTray: the tray icon's click as the OS delivers it (an OS tray click cannot be scripted).
  global.prometheosShell = { state, poll: pollEvents, clickTray: () => tray && tray.emit("click"),
                             menu: () => trayMenu };
}

if (!app.requestSingleInstanceLock()) {
  app.exit(0); // a second launch hands over to the running app (it shows its window)
} else {
  app.on("second-instance", showWindow);
  app.whenReady().then(() => {
    tray = new Tray(nativeImage.createFromPath(path.join(__dirname, "assets", "icon.png")));
    tray.on("click", showWindow);
    state.trayReady = true;
    createWindow();
    registerHotkey();
    setStatus("starting");
    setInterval(pollEvents, POLL_MS);
    return boot();
  });
  app.on("window-all-closed", (event) => event.preventDefault());
  app.on("before-quit", () => {
    state.quitting = true;
  });
  app.on("will-quit", (event) => {
    globalShortcut.unregisterAll();
    if (state.sidecar) {
      event.preventDefault();
      // The window's open connections (the hub's event stream) would hold a graceful stop.
      if (win && !win.isDestroyed()) {
        win.webContents.session.flushStorageData();
        win.destroy();
      }
      stopSidecar().then((stopped) => {
        state.stopped = stopped;
        state.settings.last_stop = { ...stopped, at: new Date().toISOString() };
        saveSettings(state.settings);
        if (tray && !tray.isDestroyed()) tray.destroy();
        app.exit(0);
      });
    }
  });
}
