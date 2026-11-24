// Negative and positive controls for the browser boundary (decision 0028). They are the
// `boundary` project of both Playwright configs, a dependency of every journey: the hub e2e suite
// (e2e/00-boundary.spec.ts) and the C10-WALK (walk/00-boundary.spec.ts) each register these, so
// each proves, on every run, the boundary the browser it is about to drive was launched with, and
// the journeys do not run when a control fails.
//
// Only reserved targets are ever requested: `.example` (RFC 2606), 192.0.2.1 (TEST-NET-1,
// RFC 5737), a `.localhost` name and 127.0.0.2 (both can only mean this machine). With the
// boundary working nothing leaves the machine. Were it missing, the `.example` lookups would reach
// the system resolver and packets for 192.0.2.1 would leave towards the gateway (nothing answers
// either); the controls exist to catch exactly that, so a failure here is a stop, not a warning.
import {
  expect,
  test,
  type PlaywrightTestArgs,
  type PlaywrightTestOptions,
  type PlaywrightWorkerArgs,
  type PlaywrightWorkerOptions,
  type TestInfo,
} from "@playwright/test";
import { BOUNDARY_ARGS, EXPECTED_BROWSER_VERSION } from "./boundary";
import { mergeOutcome, readEvidence, writeEvidence } from "./boundary-evidence";
import { SHELL_IMAGE, browserImage } from "./boundary-guard";

const NAME_PROBE = "prometheos-egress-probe.example";
const IP_PROBE = "192.0.2.1";
const RESOLVER_PROBE = "prometheos-resolver-probe.localhost";

const PROXY_FAILED = /net::ERR_PROXY_CONNECTION_FAILED/;

const firstLine = (text: string) => text.split("\n")[0] ?? "";

type ControlBody = (
  args: PlaywrightTestArgs & PlaywrightTestOptions & PlaywrightWorkerArgs & PlaywrightWorkerOptions,
  testInfo: TestInfo,
) => Promise<void> | void;

/**
 * Register the boundary controls. `hubOrigin` returns the running hub's origin without a trailing
 * slash (`http://127.0.0.1:PORT`); it is read lazily, when a test runs. When BOUNDARY_EVIDENCE
 * names a file, each control's outcome and the observed errors are merged into it as the control
 * ends (a restarted worker adds to the file instead of replacing it, and a control that ever
 * failed stays failed, even if a retry passes); `all_passed` is true only when every registered
 * control has passed.
 */
export function registerBoundaryChecks(hubOrigin: () => string): void {
  const observed: Record<string, unknown> = {};
  const controls: string[] = [];

  const control = (title: string, body: ControlBody) => {
    controls.push(title);
    test(title, body);
  };

  const record = (title: string, status: string) => {
    const target = process.env.BOUNDARY_EVIDENCE;
    if (!target) return;
    const evidence = mergeOutcome(readEvidence(target, BOUNDARY_ARGS), { title, status, observed, registered: controls });
    writeEvidence(target, evidence);
  };

  test.describe("browser boundary (decision 0028)", () => {
    // One control failing stops the rest: nothing is probed on a browser that is not the one the
    // boundary describes.
    test.describe.configure({ mode: "serial" });

    test.afterEach(async ({}, testInfo) => {
      record(testInfo.title, testInfo.status ?? "unknown");
    });

    control("the browser is Playwright's Chromium headless shell, in a fresh profile", async ({ browser, page }, testInfo) => {
      expect(browser.browserType().name()).toBe("chromium");
      expect(browser.version()).toBe(EXPECTED_BROWSER_VERSION);
      // The project configuration carries the shared boundary and nothing that undoes it.
      const use = testInfo.project.use as {
        channel?: string;
        headless?: boolean;
        launchOptions?: { args?: string[]; proxy?: unknown; executablePath?: string };
        userDataDir?: string;
      };
      expect(use.channel, "no channel: that would select an installed Edge or Chrome").toBeUndefined();
      expect(use.headless).toBe(true);
      expect(use.launchOptions?.args).toEqual([...BOUNDARY_ARGS]);
      expect(use.launchOptions?.proxy, "Playwright's proxy option would change the loopback bypass").toBeUndefined();
      expect(use.launchOptions?.executablePath).toBeUndefined();
      expect(use.userDataDir).toBeUndefined();
      // A persistent (profile directory) context has no `browser()`; this one is a launch.
      expect(page.context().browser()).not.toBeNull();
      expect(await page.context().cookies()).toEqual([]);
      const userAgent = await page.evaluate(() => navigator.userAgent);
      expect(userAgent).toContain(`HeadlessChrome/${EXPECTED_BROWSER_VERSION}`);
      expect(userAgent).not.toMatch(/Edg\//);
      // The launched binary itself: the browser process' image, from the OS.
      const image = await browserImage(browser);
      expect(image).toMatch(SHELL_IMAGE);
      observed.browser = { name: browser.browserType().name(), version: browser.version(), userAgent, image };
    });

    control("positive control: the hub loads over loopback, directly", async ({ page }) => {
      const origin = hubOrigin();
      const failed: string[] = [];
      page.on("requestfailed", (request) => failed.push(`${request.url()} ${request.failure()?.errorText}`));
      const reply = await page.goto(`${origin}/`);
      expect(reply?.status()).toBe(200);
      expect(page.url()).toBe(`${origin}/`);
      const session = await page.evaluate(async () => {
        const answer = await fetch("/api/v1/session");
        return { status: answer.status, body: (await answer.json()) as Record<string, unknown> };
      });
      expect(session.status).toBe(200);
      expect(failed).toEqual([]);
      // Loopback by name also bypasses the dead proxy: the hub answers (it may refuse the Host).
      const named = await page.goto(`${origin.replace("127.0.0.1", "localhost")}/`);
      expect(named?.status()).toBeLessThan(500);
      observed.positive = { hub: origin, status: reply?.status(), by_name_status: named?.status() };
    });

    const refused = [
      `http://${NAME_PROBE}/`,
      `https://${NAME_PROBE}/`,
      `http://${IP_PROBE}/`,
      `https://${IP_PROBE}/`,
    ];
    for (const url of refused) {
      // Exactly the proxy error, for names too: a name refused any other way (for instance
      // ERR_NAME_NOT_RESOLVED, which a real DNS lookup of `.example` also produces) would not show
      // that the request went to the dead proxy.
      control(`navigation to ${url} fails at the dead proxy`, async ({ page }) => {
        const error = await page.goto(url, { timeout: 20_000 }).then(
          () => null,
          (e: Error) => e,
        );
        expect(error, `${url} must not load`).not.toBeNull();
        expect(error?.message).toMatch(PROXY_FAILED);
        observed[`goto ${url}`] = firstLine(error?.message ?? "");
      });
    }

    control("no DNS and no implicit bypass: .localhost names and 127.0.0.2 are not resolved", async ({ page }) => {
      // Chromium sends these straight out without the proxy; only the resolver rule stops them.
      const port = new URL(hubOrigin()).port;
      for (const host of [RESOLVER_PROBE, "127.0.0.2"]) {
        const url = `http://${host}:${port}/`;
        const error = await page.goto(url, { timeout: 20_000 }).then(
          () => null,
          (e: Error) => e,
        );
        expect(error, `${url} must not load`).not.toBeNull();
        expect(error?.message).toMatch(/net::ERR_NAME_NOT_RESOLVED/);
        observed[`goto ${url}`] = firstLine(error?.message ?? "");
      }
    });

    control("an in-page fetch from a page served by the hub rejects", async ({ page }) => {
      await page.goto(`${hubOrigin()}/`);
      const outcome = await page.evaluate(async (target) => {
        try {
          await fetch(target);
          return "resolved";
        } catch (e) {
          return `rejected: ${String(e)}`;
        }
      }, `http://${IP_PROBE}/`);
      expect(outcome).toMatch(/^rejected: TypeError/);
      observed["hub page fetch"] = outcome;
    });

    control("without the hub's CSP in the way, in-page fetch and WebSocket still fail at the dead proxy", async ({ page }) => {
      // The real hub's CSP (connect-src 'self') would refuse these before any network use, so the
      // page is a loopback document on the hub's origin that carries no CSP: only the network
      // stack decides.
      const origin = hubOrigin();
      await page.route(`${origin}/__boundary_probe__`, (route) =>
        route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>probe</title>" }),
      );
      const failed = new Map<string, string>();
      page.on("requestfailed", (request) => failed.set(request.url(), request.failure()?.errorText ?? ""));
      const consoleErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      await page.goto(`${origin}/__boundary_probe__`);
      for (const url of [`http://${IP_PROBE}/`, `https://${IP_PROBE}/`, `http://${NAME_PROBE}/`, `https://${NAME_PROBE}/`]) {
        const outcome = await page.evaluate(async (target) => {
          try {
            await fetch(target);
            return "resolved";
          } catch (e) {
            return `rejected: ${String(e)}`;
          }
        }, url);
        expect(outcome, url).toMatch(/^rejected: TypeError/);
        expect(failed.get(url), `network error for ${url}`).toMatch(PROXY_FAILED);
        observed[`fetch ${url}`] = failed.get(url);
      }
      for (const url of [`ws://${IP_PROBE}/`, `wss://${IP_PROBE}/`, `ws://${NAME_PROBE}/`]) {
        const socket = await page.evaluate(
          (target) =>
            new Promise<string>((resolve) => {
              let opened = false;
              const ws = new WebSocket(target);
              ws.onopen = () => {
                opened = true;
                ws.close();
              };
              ws.onerror = () => resolve(opened ? "opened" : "error");
              ws.onclose = () => resolve(opened ? "opened" : "closed without opening");
              setTimeout(() => resolve("timeout"), 15_000);
            }),
          url,
        );
        expect(socket, url).toMatch(/^(error|closed without opening)$/);
        // The browser's own account of why: the dead proxy, not just "some error".
        const reason = `WebSocket connection to '${url}' failed: Error in connection establishment: net::ERR_PROXY_CONNECTION_FAILED`;
        await expect.poll(() => consoleErrors.some((text) => text.includes(reason)), { message: reason }).toBe(true);
        observed[`websocket ${url}`] = reason;
      }
    });
  });
}
