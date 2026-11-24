// A05, CP17.1 (C17-INVENTORY; decision 0039): the Adoption page. The owner scans the approved
// research folder; the scan runs as an operation run, ends complete with equal root digests
// before and after, and its inventory rows appear with status, type and digests. Every outcome
// is checked against stored state through the API.
import { expect, shot, test } from "./fixtures";

type Scan = {
  scan_gen: number;
  root_alias: string;
  state: string;
  root_digest_before: string | null;
  root_digest_after: string | null;
  summary: { entries: number; files_on_disk: number; by_status: Record<string, number> } | null;
};
type Files = { total: number; files: { path: string; status: string; sha256: string | null; container: string | null }[] };

test("the owner scans the research folder and reads its inventory", async ({ paired: page, hub, api }) => {
  const before = (await api.get<{ scans: Scan[] }>("/adoption/scans")).scans.length;
  await page.goto(`${hub.url}adoption`);
  await expect(page.getByRole("heading", { name: "Adoption" })).toBeVisible();
  await expect(page.getByTestId("adoption-root")).toHaveValue("research");
  await page.getByTestId("adoption-scan").click();
  await expect(page.getByTestId("adoption-run-state")).toHaveAttribute("data-state", "completed", { timeout: 60_000 });

  const scans = (await api.get<{ scans: Scan[] }>("/adoption/scans")).scans;
  expect(scans).toHaveLength(before + 1);
  const scan = scans[0];
  expect(scan.root_alias).toBe("research");
  expect(scan.state).toBe("complete");
  expect(scan.root_digest_before).toBe(scan.root_digest_after);
  const card = page.locator(`[data-testid="adoption-scan-card"][data-scan-gen="${scan.scan_gen}"]`);
  await expect(card).toHaveAttribute("data-state", "complete");
  await expect(card).toContainText("root digest equal before and after");

  const stored = await api.get<Files>(`/adoption/scans/${scan.scan_gen}/files?limit=500`);
  expect(stored.total).toBe(scan.summary?.entries);
  const guide = stored.files.find((f) => f.path === "venue-guide.md");
  expect(guide?.status).toBe("read");
  expect(guide?.container).toBe("text");
  const pdf = stored.files.find((f) => f.path === "compiled-draft.pdf");
  expect(pdf?.container).toBe("pdf");

  await card.getByTestId("adoption-open-scan").click();
  const rows = page.getByTestId("adoption-file");
  await expect(page.locator('[data-testid="adoption-file"][data-path="venue-guide.md"]')).toHaveAttribute("data-status", "read");
  await expect(page.getByTestId("adoption-counts")).toContainText(`${scan.summary?.files_on_disk} file(s) on disk`);
  await page.getByTestId("adoption-status").selectOption("read");
  await expect(rows.first()).toHaveAttribute("data-status", "read");
  for (const status of await rows.evaluateAll((els) => els.map((e) => e.getAttribute("data-status")))) {
    expect(status).toBe("read");
  }
  await shot(page, "adoption-inventory");

  // Everything the analyzers found about one file, with tiers (CP17.2)
  await page.getByTestId("adoption-status").selectOption("");
  await page.locator('[data-testid="adoption-file"][data-path="venue-guide.md"]').getByTestId("adoption-file-open").click();
  const panel = page.getByTestId("adoption-evidence-panel");
  await expect(panel).toContainText("venue-guide.md");
  const stored2 = await api.get<{ evidence: { predicate: string; tier: string }[] }>(
    `/adoption/scans/${scan.scan_gen}/evidence?path=venue-guide.md&limit=500`,
  );
  expect(stored2.evidence.length).toBeGreaterThan(0);
  await expect(panel.getByTestId("adoption-evidence-item")).toHaveCount(stored2.evidence.length);
  await expect(panel.getByTestId("adoption-evidence-item").first()).toHaveAttribute("data-tier", stored2.evidence[0].tier);
  await shot(page, "adoption-evidence");

  // Families (CP17.3): the list the page shows is the stored one
  const fams = await api.get<{ total: number; families: { family_id: string; needs_owner: boolean }[] }>(
    `/adoption/scans/${scan.scan_gen}/families?limit=500`,
  );
  const section = page.getByTestId("adoption-families-section");
  await expect(section).toBeVisible();
  // CP20.4: by default only the families that need the owner are listed; all are one click away.
  const needing = fams.families.filter((f) => f.needs_owner).length;
  await expect(section.getByTestId("adoption-family")).toHaveCount(needing);
  await expect(section.getByTestId("adoption-class-summary")).toContainText(`${needing} need you`);
  await section.getByTestId("adoption-show-all").click();
  await expect(section.getByTestId("adoption-family")).toHaveCount(fams.total);
  // Tasks the agent can do (CP17.4): the list the page shows is the stored one
  const tasks = await api.get<{ tasks: unknown[] }>(`/adoption/scans/${scan.scan_gen}/assist`);
  await expect(page.getByTestId("adoption-assist-section")).toBeVisible();
  await expect(page.getByTestId("adoption-assist-task")).toHaveCount(tasks.tasks.length);

  // The owner decides a family and accepts a baseline (CP17.5; decision 0043)
  type Family = { family_id: string; current: string | null; deciding_rule: string | null; members: { path: string; outcome: string }[] };
  type Plan = { version: number; steps: { step: { step_id: string; kind: string }; state: string; action_id: string | null }[] };
  const listed = await api.get<{ families: { family_id: string }[] }>(`/adoption/scans/${scan.scan_gen}/families?limit=500`);
  expect(listed.families.length, "the fixture folder has a family to decide").toBeGreaterThan(0);
  const target = listed.families[0].family_id;
  const fam = await api.get<Family>(`/adoption/scans/${scan.scan_gen}/families/${target}`);
  const choice = fam.members.filter((m) => m.outcome !== "copy").slice(-1)[0].path;
  await section.locator(`[data-testid="adoption-family"][data-family-id="${target}"]`).getByTestId("adoption-family-open").click();
  const detail = page.getByTestId("adoption-family-detail");
  await detail.locator(`[data-testid="adoption-family-member"][data-path="${choice}"]`).getByTestId("adoption-decide").click();
  await expect(detail.locator(`[data-testid="adoption-family-member"][data-path="${choice}"]`)).toContainText("chosen");
  await expect(detail).toContainText("You chose");
  expect(await detail.innerText()).not.toContain("\\n");
  const decided = await api.get<Family>(`/adoption/scans/${scan.scan_gen}/families/${target}`);
  expect(decided.current).toBe(choice);
  expect(decided.deciding_rule).toBe("R1");

  const planSection = page.getByTestId("adoption-plan-section");
  await planSection.getByTestId("adoption-plan-create").click();
  await expect(planSection.getByTestId("adoption-plan-create")).toHaveText("Update the plan");
  const plan = await api.get<Plan>(`/adoption/scans/${scan.scan_gen}/plan`);
  expect(plan.version).toBeGreaterThan(0);
  const live = plan.steps.filter((s) => s.state !== "superseded");
  await expect(planSection.getByTestId("adoption-plan-step")).toHaveCount(live.length);
  for (const s of live) expect(s.action_id).not.toBeNull();
  await shot(page, "adoption-plan");

  await planSection.getByTestId("adoption-baseline-accept").click();
  const accepted = planSection.getByTestId("adoption-baseline");
  await expect(accepted).toBeVisible();
  const baselineId = await accepted.getAttribute("data-baseline-id");
  const baseline = await api.get<{ sha256: string; payload: { families: { family_id: string; current: string | null }[] } }>(
    `/adoption/baselines/${baselineId}`,
  );
  expect(baseline.payload.families.find((f) => f.family_id === target)?.current).toBe(choice);
  await shot(page, "adoption-baseline");

  // The note export never targets the adopted folder: with no other approved folder the hub says
  // so (review cp17-6-a F2). Then the owner scans again (CP17.6; decision 0045).
  await expect(planSection.getByTestId("adoption-export-none")).toContainText("never writes into a folder it adopts");
  await expect(planSection.getByTestId("adoption-export")).toHaveCount(0);
  const baselines = await api.get<{ baselines: { baseline_id: string }[] }>("/adoption/baselines?root_alias=research");
  expect(baselines.baselines.map((b) => b.baseline_id)).toContain(baselineId);
  await page.getByTestId("adoption-scan").click();
  await expect
    .poll(async () => (await api.get<{ scans: Scan[] }>("/adoption/scans")).scans.filter((s) => s.state === "complete").length, {
      timeout: 60_000,
    })
    .toBe(before + 2);
  const rescans = (await api.get<{ scans: Scan[] }>("/adoption/scans")).scans;
  const rescan = rescans[0];
  expect(rescan.scan_gen).toBeGreaterThan(scan.scan_gen);
  type Drift = { counts: Record<string, number>; entries: { change: string; path: string }[] };
  const drift = await api.get<Drift>(`/adoption/scans/${rescan.scan_gen}/drift`);
  // Nothing changed in the folder: an unchanged rescan shows no differences.
  expect(drift.entries).toEqual([]);
  await page.locator(`[data-testid="adoption-scan-card"][data-scan-gen="${rescan.scan_gen}"]`).getByTestId("adoption-open-scan").click();
  const changes = page.getByTestId("adoption-changes-section");
  await expect(changes.locator('[data-against="previous"] [data-testid="adoption-drift-counts"]')).toContainText(
    "0 added, 0 changed, 0 moved, 0 deleted",
  );
  await expect(changes.locator('[data-against="baseline"]')).toBeVisible();
  await shot(page, "adoption-changes");
});

test("a scan made without the MATLAB parser says so on its card (decision 0053; response rewritten)", async ({
  paired: page,
  hub,
  api,
}) => {
  // This environment has the parser, so the stored scans name no unavailable analyzer and the
  // page shows no warning. The degraded summary itself is produced and stored by the service
  // (tests/test_cp17_parsers.py); here the real response is rewritten to carry one entry.
  type Listed = { scans: (Scan & { analysis: { analyzers_unavailable: unknown[] } | null })[] };
  const stored = await api.get<Listed>("/adoption/scans");
  const complete = stored.scans.filter((s) => s.state === "complete");
  expect(complete.length, "the first test scanned the folder").toBeGreaterThan(0);
  for (const s of complete) expect(s.analysis?.analyzers_unavailable).toEqual([]);
  await page.goto(`${hub.url}adoption`);
  await expect(page.getByTestId("adoption-scan-card").first()).toBeVisible();
  await expect(page.getByTestId("adoption-analyzer-unavailable")).toHaveCount(0);

  const degraded = complete[0].scan_gen;
  await page.route("**/api/v1/adoption/scans", async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as Listed;
    for (const s of body.scans) {
      if (s.scan_gen === degraded && s.analysis) {
        s.analysis.analyzers_unavailable = [
          {
            analyzer: "code.matlab",
            reason: "the MATLAB parser is not installed (the adoption-parsers extra)",
            install: "install prometheos[adoption-parsers] and scan again",
            files: 3,
          },
        ];
      }
    }
    await route.fulfill({ response, json: body });
  });
  // The changes card says so too (review cp17-0053-a-r2 R2-2): the newest scan has a previous one,
  // and its drift response is rewritten the same way.
  type Drift = { analysis: { analyzers_unavailable: unknown[] } };
  const real = await api.get<Drift>(`/adoption/scans/${degraded}/drift`);
  expect(real.analysis.analyzers_unavailable).toEqual([]);
  await page.route(`**/api/v1/adoption/scans/${degraded}/drift**`, async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as Drift;
    body.analysis.analyzers_unavailable = [
      {
        analyzer: "code.matlab",
        reason: "the MATLAB parser is not installed (the adoption-parsers extra)",
        install: "install prometheos[adoption-parsers] and scan again",
        files: 3,
      },
    ];
    await route.fulfill({ response, json: body });
  });
  await page.reload();
  const card = page.locator(`[data-testid="adoption-scan-card"][data-scan-gen="${degraded}"]`);
  const warning = card.getByTestId("adoption-analyzer-unavailable");
  await expect(warning).toHaveCount(1);
  await expect(warning).toContainText("code.matlab");
  await expect(warning).toContainText("not installed (the adoption-parsers extra)");
  await expect(warning).toContainText("3 file(s) were inventoried but not parsed");
  await expect(warning).toContainText("install prometheos[adoption-parsers] and scan again");
  await expect(page.getByTestId("adoption-scans").getByTestId("adoption-analyzer-unavailable")).toHaveCount(1);
  await shot(page, "adoption-parser-unavailable");

  await card.getByTestId("adoption-open-scan").click();
  const changes = page.getByTestId("adoption-changes-section");
  await expect(changes).toBeVisible();
  const drifts = changes.getByTestId("adoption-drift");
  await expect(drifts.first()).toBeVisible();
  const count = await drifts.count();
  await expect(changes.getByTestId("adoption-analyzer-unavailable")).toHaveCount(count);
  await expect(changes.getByTestId("adoption-analyzer-unavailable").first()).toContainText(
    "3 file(s) were inventoried but not parsed",
  );
  // A rewritten drift request may still be in flight (the page loads more since CP20.4): let it
  // end with this test instead of failing the next one.
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("the owner says a family is noise, then undoes it (A06.1, CP20.4)", async ({ paired: page, hub, api }) => {
  type Listed = { scans: { scan_gen: number; state: string }[] };
  const scans = (await api.get<Listed>("/adoption/scans")).scans.filter((s) => s.state === "complete");
  const gen = scans[scans.length - 1].scan_gen;
  type Fam = { family_id: string; needs_owner: boolean; class_decision: string | null };
  const before = (await api.get<{ families: Fam[] }>(`/adoption/scans/${gen}/families?limit=500`)).families;
  const target = before.find((f) => f.needs_owner);
  expect(target, "the fixture has a family that needs the owner").toBeTruthy();
  await page.goto(`${hub.url}adoption`);
  const section = page.getByTestId("adoption-families-section");
  const row = section.locator(`[data-testid="adoption-family"][data-family-id="${target!.family_id}"]`);
  await row.getByTestId("adoption-family-open").click();
  const detail = page.getByTestId("adoption-family-detail");
  await detail.getByRole("button", { name: "None of these matters" }).click();
  await expect(detail.getByTestId("adoption-family-class")).toContainText("none of these matters");
  const decided = await api.get<Fam>(`/adoption/scans/${gen}/families/${target!.family_id}`);
  expect([decided.class_decision, decided.needs_owner]).toEqual(["noise", false]);
  await expect(row).toHaveCount(0); // it left the families that need the owner
  await detail.getByTestId("adoption-classify-undo").click();
  await expect(detail.getByTestId("adoption-classify")).toBeVisible();
  const undone = await api.get<Fam>(`/adoption/scans/${gen}/families/${target!.family_id}`);
  expect([undone.class_decision, undone.needs_owner]).toEqual([null, true]);
});
