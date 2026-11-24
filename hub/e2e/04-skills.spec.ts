// Path 5: use a skill: inspect provenance, licence and recipes; preview a project-local
// install. Nothing is written to the project and nothing executes.
import { expect, shot, test } from "./fixtures";

test("path 5: inspect a skill and preview a project-local install without writing anything", async ({
  paired: page,
  hub,
  api,
}) => {
  const seqBefore = await api.seq();
  const catalogBefore = await api.get("/skills");
  const mutations: string[] = [];
  page.on("request", (r) => {
    if (r.method() !== "GET") mutations.push(`${r.method()} ${new URL(r.url()).pathname}`);
  });

  await page.goto(`${hub.url}now`);
  await page.getByRole("link", { name: "Skills", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Skills" })).toBeVisible();
  await page.getByTestId("skill-list").getByRole("link", { name: hub.seed.skill_ref }).click();
  await expect(page.getByRole("heading", { level: 1, name: `Skill: ${hub.seed.skill_ref}` })).toBeVisible();

  await expect(page.getByTestId("skill-licences")).toHaveText("MIT");
  await expect(page.getByText("Complete for every file")).toBeVisible();
  await expect(page.getByText("Origin identified: yes")).toBeVisible();
  const recipe = page.getByTestId("recipe");
  await expect(recipe).toContainText("Collect and check one piece of evidence");
  await expect(recipe).toContainText("Write an entry citing the version with an exact locator.");
  await expect(recipe).toContainText("Expected artifact: entry");

  await page.getByLabel("Coding agent").selectOption("claude-code");
  await page.getByRole("button", { name: "Preview install" }).click();
  const plan = page.getByTestId("install-plan");
  await expect(plan).toContainText("nothing was written and nothing ran");
  await expect(plan).toContainText(".claude/skills/synthetic-evidence/SKILL.md");
  await expect(plan).toContainText("Executable files: none");
  await shot(page, "skill-install-preview");

  await page.getByLabel("Coding agent").selectOption("opencode");
  await page.getByRole("button", { name: "Preview install" }).click();
  await expect(plan).toContainText(".opencode/skills/synthetic-evidence/SKILL.md");

  // Nothing written: project sequence and catalog unchanged; only the preview was POSTed.
  expect(await api.seq()).toBe(seqBefore);
  expect(await api.get("/skills")).toEqual(catalogBefore);
  expect(mutations).toEqual([
    `POST /api/v1/skills/${hub.seed.skill_ref}/install-preview`,
    `POST /api/v1/skills/${hub.seed.skill_ref}/install-preview`,
  ]);
});
