// CP20.4, the hub slice (A06.1; C20-ADOPT, C20-STATUS; decisions 0083, 0106; gap audit #16) in the
// real hub. A synthetic study folder is written into the research folder and scanned. On the
// Adoption page the owner reads the proposed outline (each item with its quote and file line, a
// track with its folders), leaves one question out, retitles the paper, chooses a question's
// tracks and confirms once; a family's class proposal from two agreeing models is shown and never
// applied. On Now, the research map shows the objective's questions, an unplaced question, the
// tracks, a link the agent placed, and a claim's verifiables in its Details. Every outcome is read
// back through the API.
import { request as pwRequest, type APIRequestContext } from "@playwright/test";
import { mkdirSync, utimesSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { expect, origin, shot, test, type Api, type HubInfo } from "./fixtures";

type AgentHub = HubInfo & { agent_token: string };
type Item = {
  item_id: string;
  kind: string;
  title: string;
  cites: { path: string; line: number | null; quote: string | null }[];
  source: string;
  addresses?: string | null;
  draws_on?: string[];
  path?: string;
  producer?: string;
};
type Outline = { scan_gen: number; sha256: string; items: Item[]; confirmed: { proposal_sha256: string } | null };
type MapQuestion = {
  entry_id: string;
  title: string;
  linked_by: string | null;
  tracks: string[];
  claims: { claim_id: string; linked_by: string | null; details: { evidence: { locator: { type: string } | null; runs: unknown[] }[] } }[];
};
type MapView = {
  objective: { entry_id: string | null; questions: MapQuestion[] };
  unplaced_questions: MapQuestion[];
  tracks: { entry_id: string; title: string }[];
};

const STUDY = "map-study";
const RQ1 = "Does flywheel storage cut the energy use of a city bus?";
const DAMPER = "Which damper keeps the ring stable at full speed?";
const CLUTCH = "What limits the regenerative torque of the clutch?";
const PAPER = "Kinematic limits of a flywheel KERS";

function agentContext(hub: AgentHub): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${hub.agent_token}` },
  });
}

/** A small study that forks by track, with a paper, notes that ask questions, one output older
 * than its script, and two competing files (synthetic, written into the fixture's folder). */
function writeStudy(hub: HubInfo): void {
  const root = join(dirname(hub.seed.latex_folder), STUDY);
  const write = (rel: string, text: string, stamp?: number) => {
    const file = join(root, rel);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, text, "utf-8");
    if (stamp) utimesSync(file, stamp, stamp);
  };
  write("README.md", `# Flywheel study (synthetic)\n\nRQ1: ${RQ1}\n\n- ${DAMPER}\n`);
  write("paper/main.tex", `\\documentclass{article}\n\\title{${PAPER}}\n\\begin{document}\n\\maketitle\n\\end{document}\n`);
  write("paper/notes.md", `Q1: ${CLUTCH}\n`);
  const old = 1_760_000_000;
  const later = 1_760_100_000;
  for (const [track, n, script, output] of [
    ["track2", 2, old, old + 10],
    ["track5", 5, later, old],
  ] as const) {
    write(`${track}/run_sim.m`, `T = table([1;${n}], 'VariableNames', {'kWh'});\nwritetable(T, 'energy.csv');\n`, script);
    write(`${track}/energy.csv`, `kWh\n1\n${n}\n`, output);
  }
  for (const [vehicle, litres] of [
    ["bus", 31],
    ["van", 12],
  ] as const) {
    write(`vehicles/${vehicle}/fuel.csv`, `vehicle,litres\n${vehicle},${litres}\n`);
  }
}

async function scan(api: Api): Promise<number> {
  type Scans = { scans: { scan_gen: number; state: string }[] };
  const before = (await api.get<Scans>("/adoption/scans")).scans.length;
  await api.post("/adoption/scans", { root_alias: "research", exclusions: [], wait_seconds: 2 });
  await expect
    .poll(
      async () => {
        const scans = (await api.get<Scans>("/adoption/scans")).scans;
        return scans.length === before + 1 ? scans[0].state : "not yet";
      },
      { timeout: 60_000 },
    )
    .toBe("complete");
  const scans = (await api.get<Scans>("/adoption/scans")).scans;
  expect(scans.length).toBe(before + 1);
  return scans[0].scan_gen;
}

const of = (outline: Outline, kind: string, title: string) => {
  const found = outline.items.find((i) => i.kind === kind && i.title === title);
  expect(found, `${kind} ${title}`).toBeTruthy();
  return found!;
};

test("the owner corrects and confirms the outline once; a class proposal stays a proposal", async ({
  paired: page,
  hub,
  api,
}) => {
  writeStudy(hub);
  const gen = await scan(api);
  const outline = await api.get<Outline>(`/adoption/scans/${gen}/outline`);
  const rq1 = of(outline, "question", RQ1);
  const damper = of(outline, "question", DAMPER);
  const clutch = of(outline, "question", CLUTCH);
  const paper = of(outline, "deliverable", PAPER);
  const track5 = of(outline, "track", "track5");
  of(outline, "track", "track2");
  expect(rq1.addresses).toBe("objective");
  expect(clutch.addresses).toBe(paper.item_id);
  expect(outline.items.filter((i) => i.kind === "stale_candidate").map((i) => i.path)).toEqual([`${STUDY}/track5/energy.csv`]);

  // The agent's two models agree on what the competing fuel files are (proposals only).
  type Fam = { family_id: string; key: string; class: string; class_decision: string | null; needs_owner: boolean };
  const fams = (await api.get<{ families: Fam[] }>(`/adoption/scans/${gen}/families?limit=500`)).families;
  const fuel = fams.find((f) => f.key.endsWith("fuel"));
  expect(fuel?.class).toBe("compete");
  const agent = await agentContext(hub as AgentHub);
  const answer = { answer: "variant", current: null, quote: { path: `${STUDY}/vehicles/bus/fuel.csv`, line: 1, text: "vehicle,litres" } };
  for (const [sample, model] of [
    [1, "model/a"],
    [2, "model/b"],
  ] as const) {
    const sent = await agent.post("/api/v1/adoption/assist/submit", {
      data: { scan_gen: gen, task_kind: "propose_family_class", subject: fuel!.key, sample, response: answer, model },
    });
    expect(sent.status(), await sent.text()).toBe(200);
  }
  await agent.dispose();

  await page.goto(`${hub.url}adoption`);
  const section = page.getByTestId("adoption-outline-section");
  const row = (item: Item) => section.locator(`[data-testid="outline-item"][data-item-id="${item.item_id}"]`);
  await expect(row(rq1)).toContainText(`RQ1: ${RQ1}`);
  await expect(row(rq1)).toContainText(`${STUDY}/README.md line 3`);
  await expect(row(rq1).getByTestId("outline-source")).toHaveText("from the folder");
  await expect(row(track5)).toContainText(`folder ${STUDY}/track5`);
  const stale = section.locator('[data-testid="outline-item"][data-kind="stale_candidate"]');
  await expect(stale).toContainText(`${STUDY}/track5/energy.csv is older than its script ${STUDY}/track5/run_sim.m`);
  await expect(section.locator('[data-testid="outline-item"][data-kind="result"]').first()).toContainText("written by");
  await expect(row(clutch).getByTestId("outline-place")).toHaveValue(paper.item_id);
  await expect(row(rq1).getByTestId("outline-place")).toHaveValue("objective");

  // The owner's corrections: leave the damper question out, retitle the paper, and say RQ1
  // draws on track 5.
  await row(damper).getByTestId("outline-keep").uncheck();
  await row(paper).getByTestId("outline-title").fill("P1: kinematic limits (synthetic)");
  await row(rq1).locator(`[data-testid="outline-track"][data-track-id="${track5.item_id}"]`).check();
  await expect(row(clutch).getByTestId("outline-place").locator("option").first()).toHaveText(
    "P1: kinematic limits (synthetic)",
  );
  const edits = {
    drop: [damper.item_id],
    titles: { [paper.item_id]: "P1: kinematic limits (synthetic)" },
    addresses: {},
    draws_on: { [rq1.item_id]: [track5.item_id] },
  };
  const counts = await api.post<{ dropped: number; records_created: number; relations: number }>(
    "/adoption/outline/preview",
    { scan_gen: gen, proposal_sha256: outline.sha256, edits },
  );
  expect(counts.dropped).toBe(1);
  const shown = section.getByTestId("outline-counts");
  await expect(shown).toContainText(`${counts.records_created} new`);
  await expect(shown).toContainText(`${counts.relations} link(s)`);
  await expect(shown).toContainText("1 left out");
  await shot(page, "29-outline");

  const before = await api.seq();
  await section.getByTestId("outline-confirm").click();
  await expect(section.getByTestId("outline-confirmed")).toContainText("Confirmed");
  await expect(section.getByTestId("outline-confirm")).toHaveCount(0);
  const confirmed = await api.get<Outline>(`/adoption/scans/${gen}/outline`);
  expect(confirmed.confirmed?.proposal_sha256).toBe(outline.sha256);
  expect(await api.seq()).toBe(before + 1); // one transaction
  const papers = await api.records<{ title: string; status: string }>("paper");
  expect(papers.filter((p) => p.body.title === "P1: kinematic limits (synthetic)").map((p) => p.body.status)).toEqual([
    "planning",
  ]);
  const questions = (await api.records<{ title: string; category: string }>("entry")).filter(
    (e) => e.body.category === "question",
  );
  expect(questions.some((q) => q.body.title === RQ1)).toBe(true);
  expect(questions.some((q) => q.body.title === DAMPER)).toBe(false);
  const map = await api.get<MapView>("/knowledge/research-map");
  const placed = map.objective.questions.find((q) => q.title === RQ1);
  expect(placed, "RQ1 addresses the objective entry").toBeTruthy();
  const track = map.tracks.find((t) => t.title === "track5");
  expect(placed!.tracks).toEqual([track!.entry_id]);
  expect(placed!.linked_by).toBe("owner_or_cli");

  // The class proposal: one line, the owner's buttons unchanged, nothing applied.
  const families = page.getByTestId("adoption-families-section");
  await families.locator(`[data-testid="adoption-family"][data-family-id="${fuel!.family_id}"]`).getByTestId("adoption-family-open").click();
  const detail = page.getByTestId("adoption-family-detail");
  await expect(detail.getByTestId("adoption-class-proposal")).toHaveText(
    `Suggested by model/a and model/b: each is current in its own context — “vehicle,litres” (${STUDY}/vehicles/bus/fuel.csv line 1). A proposal only; you decide.`,
  );
  await expect(detail.getByTestId("adoption-classify")).toBeVisible();
  const still = await api.get<Fam>(`/adoption/scans/${gen}/families/${fuel!.family_id}`);
  expect([still.class, still.class_decision, still.needs_owner]).toEqual(["compete", null, true]);
  await shot(page, "29-class-proposal");
});

test("the map shows the objective's questions, unplaced ones, tracks, agent links and verifiables", async ({
  paired: page,
  hub,
  api,
}) => {
  const map0 = await api.get<MapView>("/knowledge/research-map");
  const rq1 = map0.objective.questions.find((q) => q.title === RQ1);
  expect(rq1, "the outline test placed RQ1 under the objective").toBeTruthy();
  const transact = async (operations: unknown[]) =>
    api.post("/transactions", {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await api.seq(),
      operations,
    });
  const create = (kind: string, id: string, body: unknown) => ({
    op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body,
  }); // prettier-ignore

  // A recorded run that read the manuscript and wrote the compiled draft (synthetic).
  const input = await api.record(hub.seed.manuscript_version_id);
  const output = await api.record(hub.seed.pdf_version_id);
  await api.post("/sources/receipts", {
    title: "Synthetic compile run",
    request_id: crypto.randomUUID(),
    receipt: {
      schema_version: 1,
      observation: "reported",
      reporter: "test agent",
      command: "latexmk manuscript",
      working_root: "research",
      environment: { os: "TestOS", tools: [] },
      started_at: null,
      ended_at: null,
      exit_status: 0,
      outcome: "succeeded",
      inputs: [{ source_id: input.body.source_id, version_id: input.id, sha256: input.body.sha256, role: "source" }],
      outputs: [{ source_id: output.body.source_id, version_id: output.id, sha256: output.body.sha256, role: "draft" }],
      limitations: ["synthetic"],
    },
  });
  const claimId = `claim_${crypto.randomUUID()}`;
  const unplaced = `entry_${crypto.randomUUID()}`;
  await transact([
    create("claim", claimId, {
      statement: "The compiled draft states the uphill energy of the synthetic haul.",
      scope: {
        system: "Synthetic plant",
        operating_regime: "Mine haul",
        comparator: "Same vehicle without the device",
        model_fidelity: "Reduced model",
        validation_level: "Re-read from the stored artefact",
      },
      evidence: [{ source_id: output.body.source_id, version_id: output.id, locator: { type: "file" }, quote_sha256: null }],
      assessment_refs: [],
      state: "proposed",
      superseded_by: null,
      summary: "The draft states the uphill energy",
    }),
    create("entry", unplaced, {
      title: "Is the haul cycle representative of the fleet?",
      text: "Is the haul cycle representative of the fleet?",
      category: "question",
      evidence: [],
      claim_refs: [],
      author_ref: null,
      origin: "typed",
      quantities: [],
      review: "reviewed",
    }),
  ]);
  await transact([
    create("assessment", `assessment_${crypto.randomUUID()}`, {
      claim_ref: { id: claimId, rev: 1 },
      outcome: "supports",
      rationale: "Re-read from the stored artefact.",
      checks: [
        { name: "Uphill energy", result: "pass", evidence: [], expected: "~200 MJ", measured: "196.9 MJ", tolerance: "order of magnitude" },
      ],
      author_ref: null,
      method: "reported_agent",
      scope_match: true,
    }),
  ]);
  // The agent places the claim under RQ1 through its own token.
  const agent = await agentContext(hub as AgentHub);
  const placed = await agent.post("/api/v1/transactions", {
    data: {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await api.seq(),
      operations: [
        { op: "relation_add", relation: "answers", source_ref: { id: claimId, rev: 1 }, target_ref: { id: rq1!.entry_id, rev: 1 } },
      ],
    },
  });
  expect(placed.status(), await placed.text()).toBe(200);
  await agent.dispose();

  const map = await api.get<MapView>("/knowledge/research-map");
  const question = map.objective.questions.find((q) => q.entry_id === rq1!.entry_id)!;
  const item = question.claims.find((c) => c.claim_id === claimId)!;
  expect(item.linked_by).toBe("agent");
  expect(item.details.evidence[0].locator).toEqual({ type: "file" });
  expect(item.details.evidence[0].runs).toHaveLength(1);
  expect(map.unplaced_questions.map((q) => q.entry_id)).toContain(unplaced);

  await page.goto(hub.url);
  const overview = page.getByTestId("map-overview");
  const objective = overview.getByTestId("map-objective-questions");
  const block = objective.getByTestId("map-question").filter({ hasText: RQ1 });
  const row = block.locator(`[data-testid="map-claim"][data-linked-by="agent"]`);
  await expect(row).toContainText("The draft states the uphill energy");
  await expect(row.getByTestId("placed-by-agent")).toHaveText("placed by your agent");
  await expect(overview.getByTestId("map-unplaced-questions")).toContainText("Is the haul cycle representative of the fleet?");
  const tracks = overview.getByTestId("map-tracks");
  await expect(tracks.getByTestId("map-track").filter({ hasText: "track5" })).toContainText("1 question");
  await expect(tracks.getByTestId("map-track").filter({ hasText: "track2" })).toBeVisible();
  // Identifiers and digests stay inside the claim's Details.
  expect(await row.innerText()).not.toMatch(/\b[0-9a-f]{12,}\b/);
  await row.getByText("Details").click();
  const details = row.getByTestId("claim-details");
  const evidence = details.getByTestId("claim-evidence");
  await expect(evidence).toHaveAttribute("data-locator", "file");
  await expect(evidence).toContainText("the whole file");
  await expect(evidence).toContainText(`sha256 ${String(output.body.sha256).slice(0, 12)}`);
  await expect(evidence).toContainText(`${output.body.byte_length} bytes`);
  const run = details.getByTestId("claim-run");
  await expect(run).toContainText("latexmk manuscript");
  await expect(run).toContainText("manuscript.md");
  await expect(run).toContainText("receipt Synthetic compile run");
  const checks = details.getByTestId("claim-checks");
  await expect(checks).toContainText("~200 MJ");
  await expect(checks).toContainText("196.9 MJ");
  await expect(checks).toContainText("order of magnitude");
  await shot(page, "29-now-map");
});
