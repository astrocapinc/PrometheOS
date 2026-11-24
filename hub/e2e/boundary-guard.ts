// The browser a journey worker actually got (decision 0028). The `boundary` project proves the
// launch of its own worker; the journeys run in later workers with their own launches. Every
// journey worker therefore re-checks, once and before its first test, the launch options it was
// given (a spec-level `test.use({ launchOptions | channel | browserName })` is seen here, since
// worker options are re-resolved for that file; the whole launch options must be the boundary's,
// and no remote browser may be connected instead) and the browser it started: exact version and
// the OS image of the browser process. The result is added to the boundary evidence. Every test
// also checks, before it starts, that its browser context gets no proxy of its own: a
// context-level `proxy` (a test option, `test.use({ proxy })` or `contextOptions.proxy`) would
// replace the dead proxy for that context (review cp10-browser R3-F3).
// tools/run_hub_e2e.py and tools/walk_cp10.py fail a run in which any test outside the `boundary`
// project ran in a worker this guard did not record (a spec that imported Playwright's own `test`).
import { expect, test as base, type Browser } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readlinkSync } from "node:fs";
import { join } from "node:path";
import { BOUNDARY_ARGS, EXPECTED_BROWSER_VERSION, boundaryUse } from "./boundary";
import { readEvidence, writeEvidence } from "./boundary-evidence";

/** The headless shell image: chromium_headless_shell-<rev>/chrome-headless-shell-<platform>/chrome-headless-shell[.exe]. */
export const SHELL_IMAGE = /chromium_headless_shell-\d+[\\/]chrome-headless-shell[^\\/]*[\\/]chrome-headless-shell(\.exe)?$/;

/** The OS image of a process: what the browser process actually is, whatever Playwright reports. */
function imagePathOf(pid: number): string {
  if (process.platform === "win32") {
    const powershell = join(process.env.SystemRoot ?? "C:/WINDOWS", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    return execFileSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", `(Get-Process -Id ${pid}).Path`], {
      encoding: "utf-8",
      windowsHide: true,
    }).trim();
  }
  if (process.platform === "linux") return readlinkSync(`/proc/${pid}/exe`);
  return execFileSync("ps", ["-p", String(pid), "-o", "comm="], { encoding: "utf-8" }).trim();
}

/** The image of the browser process of `browser`, through CDP `SystemInfo.getProcessInfo`. */
export async function browserImage(browser: Browser): Promise<string> {
  const session = await browser.newBrowserCDPSession();
  try {
    const { processInfo } = (await session.send("SystemInfo.getProcessInfo" as never)) as unknown as {
      processInfo: { type: string; id: number }[];
    };
    const main = processInfo.find((p) => p.type === "browser");
    expect(main, "the browser process").toBeDefined();
    return imagePathOf(main?.id ?? 0);
  } finally {
    await session.detach();
  }
}

/** `test` for every journey: the worker's browser is checked against the boundary before it is used. */
export const guardedTest = base.extend<{ journeyContext: void }, { journeyBrowser: void }>({
  journeyContext: [
    async ({ proxy, contextOptions }, use) => {
      expect(proxy, "a context-level proxy would replace the boundary's dead proxy").toBeUndefined();
      expect(contextOptions.proxy, "a context-level proxy would replace the boundary's dead proxy").toBeUndefined();
      await use();
    },
    { auto: true },
  ],
  journeyBrowser: [
    async ({ browser, browserName, channel, connectOptions, launchOptions }, use, workerInfo) => {
      expect(browserName).toBe("chromium");
      expect(channel, "no channel: that would select an installed Edge or Chrome").toBeUndefined();
      expect(connectOptions, "a connected (remote) browser is not the boundary's").toBeUndefined();
      expect(launchOptions.args, "the boundary's launch arguments").toEqual([...BOUNDARY_ARGS]);
      expect(launchOptions.proxy, "Playwright's proxy option would change the loopback bypass").toBeUndefined();
      expect(launchOptions.executablePath).toBeUndefined();
      // Nothing else either (ignoreDefaultArgs, env, ...): exactly the boundary's launch options.
      expect(launchOptions, "the boundary's launch options, and nothing else").toStrictEqual(boundaryUse.launchOptions);
      expect(browser.version()).toBe(EXPECTED_BROWSER_VERSION);
      const image = await browserImage(browser);
      expect(image).toMatch(SHELL_IMAGE);
      const target = process.env.BOUNDARY_EVIDENCE;
      if (target) {
        const evidence = readEvidence(target, BOUNDARY_ARGS);
        evidence.journeys = evidence.journeys
          .filter((j) => !(j.project === workerInfo.project.name && j.worker === workerInfo.workerIndex))
          .concat({ project: workerInfo.project.name, worker: workerInfo.workerIndex, version: browser.version(), image });
        writeEvidence(target, evidence);
      }
      await use();
    },
    { scope: "worker", auto: true },
  ],
});
