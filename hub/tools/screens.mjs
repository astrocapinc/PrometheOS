// Screenshot gallery of a running hub (synthetic fixtures only), for design review.
//   node tools/screens.mjs <info.json> <out dir> [theme ...]
// Pairs with the next unused token from tools/hub_fixture.py's info file, then captures each
// page at 1440x900 and 800x600 in each theme (default: dark light). Loopback only.
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const [infoPath, outDir, ...themesArg] = process.argv.slice(2);
if (!infoPath || !outDir) {
  console.error("usage: node tools/screens.mjs <info.json> <out dir> [theme ...]");
  process.exit(2);
}
const info = JSON.parse(readFileSync(infoPath, "utf8"));
const base = new URL(info.url);
if (base.hostname !== "127.0.0.1") throw new Error("loopback hubs only");
const themes = themesArg.length ? themesArg : ["dark", "light"];
const pages = [
  ["now", "/now"],
  ["work", "/work"],
  ["notebook", "/notebook"],
  ["knowledge", "/knowledge"],
  ["library", "/knowledge/library"],
  ["matrix", "/knowledge/matrix"],
  ["papers", "/papers"],
  ["approvals", "/approvals"],
  ["runs", "/runs"],
  ["adoption", "/adoption"],
  ["meeting", `/meetings/${info.meeting_id}`],
  ["capture", "/capture"],
];
const sizes = [
  ["1440", { width: 1440, height: 900 }],
  ["800", { width: 800, height: 600 }],
];
const tokenIndex = Number(process.env.TOKEN_INDEX ?? info.pairing_tokens.length - 1);
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ viewport: sizes[0][1] });
  const reply = await context.request.post(new URL("/api/v1/pair", base).href, {
    data: { token: info.pairing_tokens[tokenIndex] },
    headers: { Origin: base.origin },
  });
  if (!reply.ok()) throw new Error(`pairing failed: ${reply.status()}`);
  const page = await context.newPage();
  for (const theme of themes) {
    await page.goto(new URL("/now", base).href);
    await page.evaluate((t) => window.localStorage.setItem("prometheos.hub.theme", t), theme);
    for (const [sizeName, viewport] of sizes) {
      await page.setViewportSize(viewport);
      for (const [name, path] of pages) {
        if (sizeName === "800" && !["now", "knowledge", "work"].includes(name)) continue;
        await page.goto(new URL(path, base).href);
        await page.getByRole("heading", { level: 1 }).first().waitFor({ timeout: 10000 });
        await page.locator(".state-loading").first().waitFor({ state: "detached", timeout: 5000 }).catch(() => undefined);
        await page.waitForTimeout(350);
        await page.screenshot({ path: join(outDir, `${theme}-${sizeName}-${name}.png`) });
      }
    }
    if (theme === themes[0]) {
      await page.setViewportSize(sizes[0][1]);
      await page.goto(new URL("/knowledge", base).href);
      await page.getByRole("heading", { level: 1, name: "Knowledge" }).waitFor();
      await page.keyboard.press("Control+k");
      await page.getByRole("combobox", { name: "Search pages, actions and records" }).fill("energy");
      await page.waitForTimeout(900);
      await page.screenshot({ path: join(outDir, `${theme}-1440-palette.png`) });
      await page.keyboard.press("Escape");
    }
  }
} finally {
  await browser.close();
}
console.log(`screens written to ${outDir}`);
