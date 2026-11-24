// C21-LAUNCH, the PWA (A07, CP21.3; decision 0090): the browser's own installability report.
//
//   node hub/tools/pwa-installability.mjs <hub url> <home host> <launch code> <msedge.exe>
//
// Playwright's headless shell has no installability manager (it reports no error even for a page
// without a manifest), so this one check drives the installed Microsoft Edge, headless, with a
// fresh temporary profile (never the owner's), named by its executable path (a synthetic
// environment hides the folders Playwright's "msedge" channel searches), inside the same
// loopback-only boundary as every other browser suite here (e2e/boundary.ts), widened only by
// the home's own host name. It pairs
// with the launch code in the fragment, opens record pages, and prints one JSON document:
// Edge's installability errors for the hub, the same report with the manifest removed (the
// control: it must name errors), the service worker's state and every cached path.
import { chromium } from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BOUNDARY_ARGS } from "../e2e/boundary.ts";

const [url, host, code, edge] = process.argv.slice(2);
if (!url || !host || !code || !edge) {
  throw new Error("usage: pwa-installability.mjs <url> <host> <code> <msedge.exe>");
}

const args = BOUNDARY_ARGS.map((arg) =>
  arg.startsWith("--host-resolver-rules=") ? `${arg}, EXCLUDE ${host}` : arg,
);

async function report(withManifest) {
  const profile = mkdtempSync(join(tmpdir(), "prometheos-pwa-"));
  const context = await chromium.launchPersistentContext(profile, {
    executablePath: edge,
    headless: true,
    args,
  });
  try {
    if (!withManifest) {
      await context.route("**/manifest.webmanifest", (route) => route.fulfill({ status: 404 }));
    }
    const page = await context.newPage();
    await page.goto(withManifest ? `${url}#pair=${code}` : url);
    const out = {};
    if (withManifest) {
      await page.getByRole("navigation").first().waitFor({ timeout: 30_000 });
      out.fragment_after_pairing = await page.evaluate(() => window.location.hash);
      for (const path of ["/now", "/work", "/knowledge"]) {
        await page.goto(new URL(path, url).href);
        await page.getByRole("navigation").first().waitFor({ timeout: 30_000 });
      }
      await page.evaluate(() => navigator.serviceWorker.ready);
      out.service_worker = await page.evaluate(async () => {
        const registration = await navigator.serviceWorker.getRegistration();
        return registration?.active?.state ?? "none";
      });
      out.caches = await page.evaluate(async () => {
        const found = {};
        for (const name of await caches.keys()) {
          const requests = await (await caches.open(name)).keys();
          found[name] = requests.map((request) => new URL(request.url).pathname);
        }
        return found;
      });
    } else {
      await page.waitForLoadState("networkidle");
    }
    const cdp = await context.newCDPSession(page);
    out.installability_errors = (await cdp.send("Page.getInstallabilityErrors")).installabilityErrors;
    out.browser = context.browser()?.version() ?? null;
    return out;
  } finally {
    await context.close();
    rmSync(profile, { recursive: true, force: true });
  }
}

const hub = await report(true);
const control = await report(false);
process.stdout.write(
  JSON.stringify({ ...hub, control_errors: control.installability_errors }, null, 2) + "\n",
);
