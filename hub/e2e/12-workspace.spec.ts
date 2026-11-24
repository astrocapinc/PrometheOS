// CP9 LaTeX workspace in the real hub (C09-EDITOR, S20; the hub side of C09-SYNC).
// The fixture paper is a clone of a local bare repository standing in for Overleaf, with an
// anchor binding a claim to a line of main.tex. Another "process" (this test, through the
// file system and git) edits files behind the editor's back. Every UI action is checked
// against stored state through a separately paired client, on disk and in Git.
import AxeBuilder from "@axe-core/playwright";
import type { Page, Response as HubResponse } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, shot, test } from "./fixtures";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function serious(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

const sha = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");

function git(cwd: string, ...args: string[]): string {
  return execFileSync(
    "git",
    ["-c", "user.name=Collaborator", "-c", "user.email=collab@example.invalid", ...args],
    { cwd, encoding: "utf-8" },
  ).trim();
}

// One Overleaf action runs several git processes in the hub (fetch, merge, commit, push), and on a
// loaded Windows host creating each can take seconds (review cp10-browser R4-F2). Each git-backed
// request therefore gets its own budget, longer than the hub's own 30 s git timeout so that a stuck
// git shows up as the hub's answer, and must succeed, with the hub's status and body in the failure
// message. The UI, disk and Git assertions after it are unchanged.
const GIT_BUDGET = 60_000;

function gitReply(page: Page, path: string, method = "POST"): Promise<HubResponse> {
  return page.waitForResponse((r) => r.url().endsWith(path) && r.request().method() === method, {
    timeout: GIT_BUDGET,
  });
}

async function succeeded(reply: Promise<HubResponse>, what: string): Promise<any> {
  const answer = await reply;
  const body = await answer.text();
  expect(answer.ok(), `${what}: HTTP ${answer.status()} ${body}`).toBe(true);
  return JSON.parse(body);
}

async function openWorkspace(page: Page, url: string, paperId: string) {
  await page.goto(`${url}papers/${paperId}/workspace`);
  await expect(page.getByRole("heading", { level: 1, name: "Synthetic LaTeX workspace paper" })).toBeVisible();
  await expect(page.getByTestId("save-state")).toHaveText("main.tex saved");
}

test("edit, external change refused with the draft kept, completion, compile, PDF to source", async ({
  paired: page,
  hub,
  api,
}) => {
  // Fails loudly (never skips) when the pinned engine is missing: install it with
  // `prometheos tex engine install`, which the fixture then finds.
  expect(hub.seed.tex_engine, "the real Tectonic engine is required").toBe("installed");
  const seed = hub.seed;
  const main = join(seed.latex_folder, "main.tex");
  // The editor and the PDF viewer must work under the hub's CSP (no inline styles/eval).
  const violations: string[] = [];
  page.on("console", (m) => {
    if (m.text().includes("Content Security Policy")) violations.push(m.text());
  });
  await openWorkspace(page, hub.url, seed.latex_paper_id);

  // The claim shows beside its line (gutter) and is reachable by keyboard (Claims tab).
  const mark = page.getByTestId("anchor-mark").filter({ hasText: "◆" });
  await expect(mark).toHaveCount(1);
  await expect(mark).toHaveAttribute("aria-label", /reduces settling time/);
  await page.getByRole("tab", { name: "Claims" }).focus();
  await page.keyboard.press("Enter");
  const goTo = page.getByRole("button", { name: `Go to line ${seed.latex_anchor_line}` });
  await goTo.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-activeLine")).toContainText("settling time");

  // Edit a section; another process then changes the same file on disk.
  await page.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("\n% hub draft line");
  await expect(page.getByTestId("save-state")).toHaveText("Unsaved changes in main.tex");
  appendFileSync(main, "% external edit\r\n");
  await page.keyboard.press("Control+s");
  const external = page.getByTestId("external-change");
  await expect(external).toBeVisible();
  await expect(external).toContainText("your draft is kept");
  expect(readFileSync(main, "utf-8")).toContain("% external edit");
  expect(readFileSync(main, "utf-8")).not.toContain("hub draft line");
  await expect(page.locator(".cm-content")).toContainText("% hub draft line");
  await expect(page.getByTestId("save-state")).toHaveText("Unsaved changes in main.tex");
  await shot(page, "cp9-workspace-external-change");

  // Reload the disk version, then complete a citation key by keyboard and save.
  await external.getByRole("button", { name: /Load the disk version/ }).click();
  await expect(page.locator(".cm-content")).toContainText("% external edit");
  await expect(page.locator(".cm-content")).not.toContainText("hub draft line");
  await page.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("% cited \\cite{do");
  const option = page.locator(".cm-tooltip-autocomplete li").filter({ hasText: "doe2024" });
  await expect(option).toBeVisible();
  await expect(option).toHaveAttribute("aria-selected", "true");
  // CodeMirror ignores Enter for 75 ms after the list opens (accidental-accept guard).
  await page.waitForTimeout(150);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Control+s");
  await expect(page.getByTestId("save-state")).toHaveText("main.tex saved");
  expect(readFileSync(main, "utf-8")).toContain("% cited \\cite{doe2024}");

  // Compile; the PDF renders and the anchor follows the remap rules.
  await page.getByRole("button", { name: "Compile", exact: true }).click();
  await expect(page.getByTestId("compile-state")).toHaveText("succeeded", { timeout: 60_000 });
  await expect(page.locator("[data-testid=pdf-page] canvas[data-rendered=true]").first()).toBeVisible();
  const files = await api.get(`/papers/${seed.latex_paper_id}/workspace/files`);
  const mainFile = files.files.find((f: { path: string }) => f.path === "main.tex");
  expect(mainFile.state).toBe("versioned");
  expect(mainFile.sha256).toBe(sha(main));
  const status = await api.get(`/papers/${seed.latex_paper_id}/compile`);
  expect(status.last_compile.pdf_current).toBe(true);
  expect(status.pdf.transformation.input_version_refs).toContain(mainFile.latest_version_id);
  const anchor = await api.record(seed.latex_anchor_id);
  expect(anchor.body.manuscript_version_id).toBe(mainFile.latest_version_id);
  expect(anchor.body.status).toBe("current");

  // Source to PDF and back.
  await page.getByRole("tab", { name: "Claims" }).click();
  await page.getByRole("button", { name: `Go to line ${seed.latex_anchor_line}` }).click();
  await page.getByRole("button", { name: "Show line in PDF" }).click();
  const highlight = page.getByTestId("pdf-highlight").first();
  await expect(highlight).toBeVisible();
  await page.getByRole("tab", { name: "Outline" }).click();
  await page.getByRole("button", { name: "Introduction" }).click();
  await expect(page.locator(".cm-activeLine")).not.toContainText("settling time");
  // Measure just before clicking: notices above the PDF can move it.
  const box = await highlight.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await expect(page.locator(".cm-activeLine")).toContainText("settling time");
  await shot(page, "cp9-workspace-compiled");
  expect(await serious(page)).toEqual([]);
  expect(violations).toEqual([]);
});

test("diagnostics, outline and a failed compile that keeps the previous PDF", async ({
  paired: page,
  hub,
  api,
}) => {
  // Fails loudly (never skips) when the pinned engine is missing: install it with
  // `prometheos tex engine install`, which the fixture then finds.
  expect(hub.seed.tex_engine, "the real Tectonic engine is required").toBe("installed");
  const seed = hub.seed;
  const intro = join(seed.latex_folder, "sections", "intro.tex");
  const original = readFileSync(intro);
  const before = await api.get(`/papers/${seed.latex_paper_id}/compile`);
  expect(before.pdf).not.toBeNull();
  try {
    appendFileSync(intro, "\\undefinedcommandxyz\r\n");
    await openWorkspace(page, hub.url, seed.latex_paper_id);
    await page.getByRole("button", { name: "Compile", exact: true }).click();
    await expect(page.getByTestId("compile-state")).toHaveText("failed", { timeout: 60_000 });
    await expect(page.getByTestId("stale-pdf")).toContainText("previous PDF");
    const lines = readFileSync(intro, "utf-8").split("\n").length - 1;
    const jump = page.getByTestId("diagnostics").getByRole("button", { name: `sections/intro.tex:${lines}` });
    await jump.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("save-state")).toHaveText("sections/intro.tex saved");
    await expect(page.locator(".cm-activeLine")).toContainText("undefinedcommandxyz");
    const after = await api.get(`/papers/${seed.latex_paper_id}/compile`);
    expect(after.last_compile.status).toBe("failed");
    expect(after.pdf.version_id).toBe(before.pdf.version_id);
    // Outline navigation by keyboard.
    await page.getByRole("tab", { name: "Outline" }).focus();
    await page.keyboard.press("Enter");
    const results = page.getByTestId("outline").getByRole("button", { name: "Results" });
    await results.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".cm-activeLine")).toContainText("\\section{Results}");
    expect(await serious(page)).toEqual([]);
  } finally {
    writeFileSync(intro, original);
  }
});

test("Overleaf: pull with preview, local commit and an approved push", async ({ paired: page, hub, api }) => {
  test.setTimeout(180_000); // five git-backed requests, each with its own budget
  const seed = hub.seed;
  // A collaborator commits on "Overleaf".
  const bib = join(seed.latex_collaborator, "refs.bib");
  writeFileSync(bib, readFileSync(bib, "utf-8").replace("Fixture Press", "Fixture Press Ltd"));
  git(seed.latex_collaborator, "commit", "-qam", "Collaborator updates the publisher");
  git(seed.latex_collaborator, "push", "-q", "origin", "master");

  const statusReply = gitReply(page, "/overleaf", "GET");
  await page.goto(`${hub.url}papers/${seed.latex_paper_id}/workspace`);
  await succeeded(statusReply, "overleaf status");
  const panel = page.getByTestId("overleaf-panel");
  await expect(panel.getByTestId("overleaf-status")).toContainText("master");
  // The cards show exactly the destination the plan's digest binds (review INT2-F3); the
  // hub may receive no URL for a local repository, and then says so.
  const shownDestination = (plan: { remote_url?: string }) => plan.remote_url ?? "a local repository";
  const pullReply = gitReply(page, "/overleaf/pull/preview");
  await panel.getByRole("button", { name: "Check Overleaf for changes" }).click();
  const pullPlan = (await succeeded(pullReply, "pull/preview")).plan;
  const preview = panel.getByTestId("pull-preview");
  await expect(preview.getByTestId("pull-source")).toContainText(shownDestination(pullPlan));
  await expect(preview).toContainText("Collaborator updates the publisher");
  await expect(preview).toContainText("refs.bib");
  expect(readFileSync(join(seed.latex_folder, "refs.bib"), "utf-8")).not.toContain("Fixture Press Ltd");
  const applied = gitReply(page, "/overleaf/pull/apply");
  await preview.getByRole("button", { name: "Pull these changes" }).click();
  await succeeded(applied, "pull/apply");
  await expect(panel.getByTestId("overleaf-done")).toContainText("Pulled");
  expect(readFileSync(join(seed.latex_folder, "refs.bib"), "utf-8")).toContain("Fixture Press Ltd");

  // Commit the workspace edits locally, then push exactly the reviewed plan.
  const tracked = git(seed.latex_folder, "status", "--porcelain");
  if (tracked === "") appendFileSync(join(seed.latex_folder, "main.tex"), "% push test\r\n");
  await panel.getByLabel("Commit message for local changes").fill("Edits from the hub");
  const committed = gitReply(page, "/overleaf/commit");
  await panel.getByRole("button", { name: "Commit locally" }).click();
  await succeeded(committed, "commit");
  await expect(panel.getByTestId("overleaf-done")).toContainText("Committed locally");
  const remoteBefore = git(seed.latex_bare, "rev-parse", "refs/heads/master");
  const pushReply = gitReply(page, "/overleaf/push/preview");
  await panel.getByRole("button", { name: "Prepare a push" }).click();
  const pushPlan = (await succeeded(pushReply, "push/preview")).plan;
  const push = panel.getByTestId("push-preview");
  await expect(push).toContainText("Edits from the hub");
  await expect(push.getByTestId("push-destination")).toContainText(
    `Destination: ${shownDestination(pushPlan)}`,
  );
  expect(git(seed.latex_bare, "rev-parse", "refs/heads/master")).toBe(remoteBefore);
  const pushed = gitReply(page, "/overleaf/push/apply");
  await push.getByRole("button", { name: /Approve and push exactly these 1 commit/ }).click();
  await succeeded(pushed, "push/apply");
  await expect(panel.getByTestId("overleaf-done")).toContainText("no force");
  await expect(panel.getByTestId("overleaf-done")).toContainText(shownDestination(pushPlan));
  const local = git(seed.latex_folder, "rev-parse", "HEAD");
  expect(git(seed.latex_bare, "rev-parse", "refs/heads/master")).toBe(local);
  git(seed.latex_bare, "merge-base", "--is-ancestor", remoteBefore, local); // fast-forward only
  const status = await api.get(`/papers/${seed.latex_paper_id}/overleaf`);
  expect(status.ahead).toBe(0);
  expect(status.behind).toBe(0);
  await shot(page, "cp9-workspace-overleaf");
  expect(await serious(page)).toEqual([]);
});

test("Overleaf: the cards show invisible characters as escapes", async ({ paired: page, hub }) => {
  test.setTimeout(180_000); // three git-backed requests, each with its own budget
  // The server refuses such destinations (pre-check round 2 INT2G-F2), but commit subjects,
  // authors and file names come from agents and collaborators (pre-check round 3 R2-L1): the
  // cards escape whatever arrives, so they read as exactly what they are.
  const RLO = String.fromCodePoint(0x202e);
  const SHOWN = String.fromCharCode(92) + "u{202e}";
  await page.route("**/overleaf/push/preview", async (route) => {
    const response = await route.fetch({ timeout: GIT_BUDGET });
    // Say what the hub answered instead of failing later on `body.plan` of an error body.
    expect(response.ok(), `${route.request().url()}: HTTP ${response.status()} ${await response.text()}`).toBe(true);
    const body = await response.json();
    body.plan.remote_url = `https://git.overleaf.com/abc${RLO}def`;
    body.plan.commits = [{ oid: "0".repeat(40), author: "x", date: "", subject: `Fix ${RLO}evil` }];
    await route.fulfill({ response, json: body });
  });
  await page.route("**/overleaf/pull/preview", async (route) => {
    const response = await route.fetch({ timeout: GIT_BUDGET });
    // Say what the hub answered instead of failing later on `body.plan` of an error body.
    expect(response.ok(), `${route.request().url()}: HTTP ${response.status()} ${await response.text()}`).toBe(true);
    const body = await response.json();
    body.plan.incoming_commits = [
      { oid: "1".repeat(40), author: `Coll${RLO}ab`, date: "", subject: `Edit ${RLO}text` },
    ];
    body.plan.incoming_files = [{ status: "M", path: `sec${RLO}tion.tex` }];
    await route.fulfill({ response, json: body });
  });
  const statusReply = gitReply(page, "/overleaf", "GET");
  await page.goto(`${hub.url}papers/${hub.seed.latex_paper_id}/workspace`);
  await succeeded(statusReply, "overleaf status");
  const panel = page.getByTestId("overleaf-panel");
  const pushReply = gitReply(page, "/overleaf/push/preview");
  await panel.getByRole("button", { name: "Prepare a push" }).click();
  await succeeded(pushReply, "push/preview");
  const push = panel.getByTestId("push-preview");
  await expect(push.getByTestId("push-destination")).toContainText(
    `Destination: https://git.overleaf.com/abc${SHOWN}def`,
  );
  await expect(push).toContainText(`Fix ${SHOWN}evil`);
  const pullReply = gitReply(page, "/overleaf/pull/preview");
  await panel.getByRole("button", { name: "Check Overleaf for changes" }).click();
  await succeeded(pullReply, "pull/preview");
  const pull = panel.getByTestId("pull-preview");
  await expect(pull).toContainText(`Edit ${SHOWN}text`);
  await expect(pull).toContainText(`Coll${SHOWN}ab`);
  await expect(pull).toContainText(`sec${SHOWN}tion.tex`);
  for (const card of [push, pull]) expect(await card.textContent()).not.toContain(RLO);
});
