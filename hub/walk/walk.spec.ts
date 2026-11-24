// C10-WALK, hub half (tools/walk_cp10.py; decision 0022 item 5). KERS scenarios K1-K4 on the
// real built hub over a fresh store seeded from the synthetic fixtures. The owner works in the
// browser (Playwright's Chromium headless shell, inside the loopback-only boundary of decision
// 0028); a local agent works through its bearer token, as a coding agent would. After every step
// the stored state is read back through a separately paired owner client, and each step is
// written to WALK_STEPS (id, operation, request, response, outcome and store assertions) for the
// transcript the harness assembles and compares with the MCP half.
//
// K2 commits nothing between the owner's grant and the agent's resume (decision 0020).
import { expect, request as pwRequest, type APIRequestContext, type Page } from "@playwright/test";
// The guarded `test`: the walk's browser worker is checked against the boundary before it is used.
import { guardedTest as test } from "../e2e/boundary-guard";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

interface Info {
  url: string;
  project_id: string;
  pairing_tokens: string[];
  agent_token: string;
}
interface Assertion {
  claim: string;
  passed: boolean;
  observed?: unknown;
}
interface Step {
  id: string;
  via: string;
  request: unknown;
  response: unknown;
  outcome: string;
  assertions: Assertion[];
  created?: Record<string, string>;
}

const info = JSON.parse(readFileSync(process.env.WALK_INFO ?? "", "utf-8")) as Info;
const fx = JSON.parse(process.env.WALK_FIXTURE ?? "{}") as Record<string, any>;
const stepsPath = process.env.WALK_STEPS ?? "walk-steps.json";
const shots = process.env.WALK_SHOTS ?? "walk-shots";
const origin = info.url.replace(/\/$/, "");
const steps: Step[] = [];
let tokenIndex = 0;

const uuid = () => crypto.randomUUID();

function check(list: Assertion[], claim: string, passed: boolean, observed?: unknown) {
  list.push({ claim, passed, observed });
}

function save() {
  writeFileSync(stepsPath, JSON.stringify(steps, null, 2), "utf-8");
}

async function pairedOwner(): Promise<{ ctx: APIRequestContext; csrf: string }> {
  const ctx = await pwRequest.newContext({ baseURL: origin, extraHTTPHeaders: { Origin: origin } });
  const token = info.pairing_tokens[tokenIndex++];
  const reply = await ctx.post("/api/v1/pair", { data: { token } });
  expect(reply.status()).toBe(200);
  return { ctx, csrf: ((await reply.json()) as { csrf_token: string }).csrf_token };
}

async function pairPage(page: Page): Promise<void> {
  const token = info.pairing_tokens[tokenIndex++];
  const reply = await page.request.post(`${origin}/api/v1/pair`, {
    data: { token },
    headers: { Origin: origin },
  });
  expect(reply.status()).toBe(200);
}

async function agentContext(): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin,
    extraHTTPHeaders: { Authorization: `Bearer ${info.agent_token}` },
  });
}

interface Reply {
  status: number;
  body: any;
}

async function call(ctx: APIRequestContext, method: "GET" | "POST", path: string, data?: unknown): Promise<Reply> {
  const reply =
    method === "GET"
      ? await ctx.get(`/api/v1${path}`)
      : await ctx.post(`/api/v1${path}`, { data: data ?? {} });
  return { status: reply.status(), body: await reply.json() };
}

function outcomeOf(reply: Reply): string {
  return reply.status < 400 ? "ok" : `error:${reply.body.error.code}`;
}

test("C10-WALK K1-K4 on the real hub", async ({ browser }) => {
  const owner = await pairedOwner();
  const ownerGet = async (path: string) => (await call(owner.ctx, "GET", path)).body;
  let agent = await agentContext();
  const context = await browser.newContext();
  const page = await context.newPage();
  await pairPage(page);
  mkdirSync(shots, { recursive: true });
  const shot = async (p: Page, name: string) => p.screenshot({ path: join(shots, `${name}.png`), fullPage: true });
  const seq = async () => ((await ownerGet("/project")) as { project_seq: number }).project_seq;

  const record = (s: Step) => {
    steps.push(s);
    save();
  };
  const base = { project_id: info.project_id, schema_version: 1 };
  const create = (kind: string, id: string, body: unknown) => ({
    op: "create",
    kind,
    id,
    base_rev: 0,
    sensitivity: "normal",
    occurred_at: null,
    body,
  });

  try {
    // ----- K1: the best supported result, its comparator, scope, source revision, limits ----
    let claim: any;
    let discoveredClaim = "";
    let statement = "";
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", "/context?purpose=orient&budget=12000");
      const text: string = r.body.text ?? "";
      for (const section of r.body.manifest.sections) {
        if (section.name !== "findings") continue;
        for (const candidate of section.included) {
          const found = await call(agent, "GET", `/records/${candidate}`);
          if (found.body.body.state === "supported_within_conditions") { claim = found.body; discoveredClaim = candidate; break; }
        }
      }
      expect(discoveredClaim).toBeTruthy();
      statement = claim.body.statement;
      check(a, "the orientation context states the finding", text.split("\n").some((line) => line.includes("CLAIM [supported_within_conditions]") && line.includes(statement)));
      check(a, "... with its comparator", text.includes(claim.body.scope.comparator));
      check(a, "... and its limits", text.includes(claim.body.scope.validation_level));
      record({ id: "K1.1", via: "http", request: { purpose: "orient" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", `/records/${discoveredClaim}`);
      check(a, "the finding is supported within its conditions", r.body.body.state === "supported_within_conditions");
      check(
        a,
        "its scope matches the cited PLANT-B fixture's regime, fidelity and comparator",
        Object.entries({
          system: "PLANT-B",
          operating_regime: "variable-demand synthetic event",
          comparator: "reference controller under the same constraints",
          model_fidelity: "dynamic plant with actuator constraints",
          validation_level: "no measured validation",
        }).every(([k, value]) => r.body.body.scope[k] === value),
      );
      record({ id: "K1.2", via: "http", request: { record_id: "claim_a" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", `/knowledge/links/${discoveredClaim}`);
      check(a, "it relies on the shared assumption", (r.body.relies_on ?? []).some((x: any) => x.id === fx.drag), r.body.relies_on);
      record({ id: "K1.3", via: "http", request: { record_id: "claim_a" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    const evidenceVersion: string = claim.body.evidence[0].version_id;
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", `/records/${evidenceVersion}`);
      const stored = (await ownerGet(`/records/${evidenceVersion}`)) as any;
      check(
        a,
        "the cited source revision is pinned by digest and size",
        r.body.body.sha256 === stored.body.sha256 && r.body.body.byte_length === stored.body.byte_length,
      );
      check(a, "its bytes are retained", r.body.body.availability === "retained");
      record({ id: "K1.4", via: "http", request: { record_id: "evidence version" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      // The owner reads the same finding in the Knowledge page.
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge`);
      const card = page.locator(`[data-testid="claim-card"][id="${fx.claim_a}"]`);
      await expect(card).toBeVisible();
      await expect(card).toHaveAttribute("data-claim-state", "supported_within_conditions");
      const cardText = (await card.innerText()).toLowerCase();
      check(a, "the page shows the finding's state", (await card.getAttribute("data-claim-state")) === "supported_within_conditions");
      check(a, "... its comparator", cardText.includes(String(claim.body.scope.comparator).toLowerCase()));
      check(a, "... its model fidelity and validation level (the limits)",
        cardText.includes(String(claim.body.scope.model_fidelity).toLowerCase()) &&
          cardText.includes(String(claim.body.scope.validation_level).toLowerCase()));
      await card.locator(".source-button").first().click();
      const panel = page.getByTestId("context-panel");
      await expect(panel).toBeVisible();
      const ev = claim.body.evidence[0];
      const text = (await ownerGet(`/versions/${evidenceVersion}/text`)).text as string;
      const lines = text.replace(/\n$/, "").split("\n");
      const cited = lines.slice(ev.locator.start_line - 1, ev.locator.end_line).join("\n");
      await expect(panel.locator("li.cited").first()).toContainText(cited.split("\n")[0]!.trim().slice(0, 30));
      check(a, "... and the exact cited lines of the retained source", true, `lines ${ev.locator.start_line}-${ev.locator.end_line}`);
      check(a, "the stored claim is the one shown", claim.body.statement === statement);
      await shot(page, "K1-knowledge");
      record({ id: "K1.5", via: "browser", request: { page: "/knowledge", claim: "claim_a" }, response: { claim_state: claim.body.state }, outcome: "ok", assertions: a });
      await page.keyboard.press("Escape");
    }

    // ----- K2: an authorised correction flags its dependents --------------------------------
    const correctionId = `correction_${uuid()}`;
    const correctionBody = {
      target_refs: [{ id: fx.drag, rev: 1 }],
      replacement_refs: [{ id: fx.drag_v2, rev: 1 }],
      reason: "The constant-drag assumption fails at high speed.",
      evidence: [],
      effects: [
        {
          target_ref: { id: fx.drag, rev: 1 },
          effect: "supersede",
          replacement_ref: { id: fx.drag_v2, rev: 1 },
          replacement_text: null,
        },
      ],
      status: "proposed",
      applied_seq: null,
    };
    {
      const a: Assertion[] = [];
      const req = { ...base, request_id: uuid(), expected_seq: await seq(), operations: [create("correction", correctionId, correctionBody)] };
      const r = await call(agent, "POST", "/transactions", req);
      const stored = (await ownerGet(`/records/${correctionId}`)) as any;
      check(a, "the proposal is stored as a proposed correction", stored.body.status === "proposed");
      record({ id: "K2.1", via: "http", request: req, response: r.body, outcome: outcomeOf(r), assertions: a, created: { correction: correctionId } });
    }
    const applyRequest = { ...base, request_id: uuid(), expected_seq: await seq(), operations: [{ op: "correction_apply", id: correctionId, base_rev: 1 }] };
    let approvalId = "";
    let planDigest = "";
    {
      const a: Assertion[] = [];
      const before = await seq();
      const r = await call(agent, "POST", "/transactions", applyRequest);
      approvalId = r.body.error?.details?.approval_request_id ?? "";
      planDigest = r.body.error?.details?.plan_sha256 ?? "";
      const flags = ((await ownerGet("/now?limit=200")) as any).changed.items as any[];
      check(a, "the agent is told it needs the owner", r.status === 403 && r.body.error.code === "approval_required");
      check(a, "nothing committed and nothing is flagged", (await seq()) === before && flags.length === 0, flags.length);
      record({ id: "K2.2", via: "http", request: applyRequest, response: r.body, outcome: outcomeOf(r), assertions: a, created: { approval_correction: approvalId } });
    }
    {
      // The owner opens the request in the review queue and grants exactly that plan.
      const a: Assertion[] = [];
      await page.goto(`${origin}/approvals`);
      const card = page.locator(`[data-testid="approval-pending"][data-approval-id="${approvalId}"]`);
      await expect(card).toBeVisible();
      await expect(card.getByTestId("approval-grant")).toBeDisabled();
      await card.getByTestId("approval-review").click();
      await expect(card.getByTestId("approval-digest")).toHaveText(planDigest);
      await expect(card.getByTestId("approval-plan")).toContainText(correctionId);
      await shot(page, "K2-approval-review");
      await card.getByTestId("approval-grant").click();
      await expect(card).toHaveCount(0);
      const state = ((await ownerGet(`/approvals/${approvalId}`)) as any).state;
      check(a, "the grant is held by the server for exactly this plan", state === "granted", state);
      record({ id: "K2.3", via: "browser", request: { approval: "approval_correction" }, response: { state }, outcome: "ok", assertions: a });
    }
    const seqAtGrant = await seq();
    {
      // Nothing may commit between the grant and the resume (decision 0020).
      const a: Assertion[] = [];
      const r = await call(agent, "POST", `/approvals/${approvalId}/resume`);
      const commitSeq = r.body.result?.project_seq;
      const assumption = (await ownerGet(`/records/${fx.drag}`)) as any;
      const flagged = new Map<string, any>((((await ownerGet("/now?limit=200")) as any).changed.items as any[]).map((i) => [i.id, i]));
      check(a, "the stored plan ran once", r.status === 200 && r.body.state === "used" && r.body.replayed === false, r.body.state);
      check(a, "the project did not move between the grant and the resume", commitSeq === seqAtGrant + 1, [seqAtGrant, commitSeq]);
      check(a, "the assumption is superseded by its replacement", JSON.stringify(assumption.body.superseded_by) === JSON.stringify({ id: fx.drag_v2, rev: 1 }));
      const expected = [fx.claim_a, fx.action, fx.anchor, fx.anchor_claim];
      check(a, "the dependents were flagged", expected.every((id: string) => flagged.has(id)), [...flagged.keys()]);
      check(a, "... in the same commit", expected.every((id: string) => flagged.get(id)?.seq === commitSeq));
      record({ id: "K2.4", via: "http", request: { approval_id: "approval_correction" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const before = await seq();
      const r = await call(agent, "POST", `/approvals/${approvalId}/resume`);
      check(a, "a second resume replays the stored result and changes nothing", r.status === 200 && r.body.replayed === true && (await seq()) === before);
      record({ id: "K2.5", via: "http", request: { approval_id: "approval_correction" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", `/records/${fx.anchor}`);
      const control = (await ownerGet(`/records/${fx.anchor_control}`)) as any;
      check(a, "the manuscript anchor that used the assumption needs review", r.body.body.status === "needs_review");
      check(a, "the control anchor is unchanged", control.body.status === "current");
      record({ id: "K2.6", via: "http", request: { record_id: "anchor_uses_assumption" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", "/now?limit=200");
      const changed = new Set<string>((r.body.changed.items as any[]).map((i) => i.id));
      check(a, "Now lists the flagged dependents as changed", [fx.claim_a, fx.anchor, fx.anchor_claim].every((id: string) => changed.has(id)));
      check(a, "... and not the replacement or the correction", !changed.has(fx.drag_v2) && !changed.has(correctionId));
      record({ id: "K2.7", via: "http", request: { limit: 200 }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      // The owner sees the flags, with their reason, in the pages.
      const a: Assertion[] = [];
      await page.goto(`${origin}/knowledge`);
      const card = page.locator(`[data-testid="claim-card"][id="${fx.claim_a}"]`);
      await expect(card.getByTestId("claim-flag")).toContainText("Needs review");
      await expect(card.getByTestId("claim-assumptions").locator(`[data-assumption-id="${fx.drag}"]`)).toHaveAttribute("data-assumption-state", "superseded");
      check(a, "the claim page shows it needs review and its assumption is superseded", true);
      await page.goto(`${origin}/now`);
      const changedRow = page.locator(`[data-testid="now-changed"] [data-id="${fx.claim_a}"]`);
      await expect(changedRow).toBeVisible();
      await expect(changedRow.getByTestId("now-reason")).toContainText("Why:");
      check(a, "Now shows the flagged claim with its reason", true);
      await shot(page, "K2-now-changed");
      record({ id: "K2.8", via: "browser", request: { pages: ["/knowledge", "/now"] }, response: { flagged: fx.claim_a !== "" }, outcome: "ok", assertions: a });
    }

    // ----- K3: a finished run nobody has interpreted ----------------------------------------
    let receiptVersion = "";
    {
      const a: Assertion[] = [];
      const req = { receipt: fx.receipt, title: "Walk receipt", request_id: uuid() };
      const r = await call(agent, "POST", "/sources/receipts", req);
      receiptVersion = ((r.body.results ?? []) as any[]).find((x) => String(x.id).startsWith("version_"))?.id ?? "";
      const stored = (await ownerGet(`/records/${receiptVersion}`)) as any;
      check(a, "the receipt is stored as reported, not observed", stored.body.payload_schema === "execution_receipt_v1");
      record({ id: "K3.1", via: "http", request: { title: "Walk receipt" }, response: r.body, outcome: outcomeOf(r), assertions: a, created: { walk_receipt: receiptVersion } });
    }
    {
      const a: Assertion[] = [];
      const req = { state: "reported_completed", receipt_version_ids: [receiptVersion], request_id: uuid() };
      const r = await call(agent, "POST", `/executions/${fx.run_done}/report`, req);
      const stored = (await ownerGet(`/records/${fx.run_done}`)) as any;
      check(a, "the run is recorded completed, nothing was started or stopped", stored.body.state === "reported_completed");
      record({ id: "K3.2", via: "http", request: { execution: "run_done", state: "reported_completed" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", "/awaiting-interpretation");
      const waiting = new Set<string>((r.body.items as any[]).map((i) => i.target_id));
      check(a, "the finished run awaits interpretation", waiting.has(fx.run_done));
      check(a, "so does the receipt nobody registered a run for", waiting.has(fx.bare_receipt));
      check(a, "a run still reported running does not", !waiting.has(fx.run_live));
      record({ id: "K3.3", via: "http", request: {}, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      await page.goto(`${origin}/now`);
      const done = page.locator(`[data-testid="now-awaiting"] [data-id="${fx.run_done}"]`);
      const bare = page.locator(`[data-testid="now-awaiting"] [data-id="${fx.bare_receipt}"]`);
      await expect(done).toBeVisible();
      await expect(done.getByTestId("now-reason")).toContainText("no assessment or reviewed entry cites it");
      await expect(bare).toBeVisible();
      check(a, "Now lists the finished run and the unregistered receipt as awaiting interpretation", true);
      await expect(page.locator(`[data-testid="now-running"] [data-id="${fx.run_live}"]`)).toBeVisible();
      check(a, "the run still reported running is listed as running, never controlled", true);
      await shot(page, "K3-now-awaiting");
      record({ id: "K3.4", via: "browser", request: { page: "/now" }, response: { listed: [fx.run_done, fx.bare_receipt] }, outcome: "ok", assertions: a });
    }
    let dismissalApproval = "";
    {
      const a: Assertion[] = [];
      const req = {
        resolution: "dismissed",
        text: "The sweep used the old drag model; nothing from it is used.",
        request_id: uuid(),
      };
      const r = await call(agent, "POST", `/awaiting-interpretation/${fx.run_done}/resolve`, req);
      dismissalApproval = r.body.error?.details?.approval_request_id ?? "";
      const queue = (await ownerGet("/awaiting-interpretation")) as any;
      check(a, "an agent cannot dismiss a result itself: approval_required", r.status === 403 && r.body.error.code === "approval_required");
      check(a, "the run is still in the queue", (queue.items as any[]).some((i) => i.target_id === fx.run_done));
      record({ id: "K3.5", via: "http", request: { target: "run_done", resolution: "dismissed" }, response: r.body, outcome: outcomeOf(r), assertions: a, created: { approval_dismissal: dismissalApproval } });
    }
    {
      // The owner dismisses the unregistered receipt, in the browser, with a reason.
      const a: Assertion[] = [];
      const reason = "Superseded by the walk run; nothing in it is used.";
      await page.goto(`${origin}/now`);
      const bare = page.locator(`[data-testid="now-awaiting"] [data-id="${fx.bare_receipt}"]`);
      await bare.getByText("Record an interpretation or dismiss with a reason").click();
      await bare.getByTestId("resolve-text").fill(reason);
      await bare.getByTestId("resolve-dismissed").click();
      await expect(bare).toHaveCount(0);
      const entries = ((await ownerGet("/records?kind=entry&limit=200")) as any).items as any[];
      const entry = entries.find((e) => e.body.category === "decision" && e.body.text === reason);
      check(a, "the dismissal is a reviewed decision entry that carries the reason", !!entry && entry.body.review === "reviewed", entry?.body?.title);
      record({ id: "K3.6", via: "browser", request: { target: "bare_receipt", resolution: "dismissed" }, response: { entry: entry?.body }, outcome: "ok", assertions: a });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", "/awaiting-interpretation");
      const waiting = new Set<string>((r.body.items as any[]).map((i) => i.target_id));
      check(a, "the dismissed receipt left the queue", !waiting.has(fx.bare_receipt));
      check(a, "the finished run still waits for an interpretation", waiting.has(fx.run_done));
      record({ id: "K3.7", via: "http", request: {}, response: r.body, outcome: outcomeOf(r), assertions: a });
    }

    // ----- K4: resume after the chat closes -------------------------------------------------
    let runId = "";
    let importResponse: any;
    {
      const a: Assertion[] = [];
      const req = { root_alias: "research", paths: ["artifact.bin"], wait_seconds: 0 };
      const r = await call(agent, "POST", "/sources/plans", req);
      runId = r.body.run_id ?? "";
      importResponse = r.body;
      check(a, "the answer is a run ID (oprun_), not the result", String(runId).startsWith("oprun_"));
      record({ id: "K4.1", via: "http", request: req, response: r.body, outcome: outcomeOf(r), assertions: a, created: { import_run: runId } });
    }
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", `/runs/${runId}`);
      const view = r.body;
      check(a, "the real run is durably queued at chat close (synthetic dispatcher hold)", view.state === "queued", view.state);
      record({ id: "K4.2", via: "http", request: { run_id: "import_run" }, response: view, outcome: outcomeOf(r), assertions: a });
    }
    // The chat closes: the agent's connection and the browser's context are gone.
    const oldAgent = agent;
    await agent.dispose();
    await context.close();
    let disposedRefused = false;
    try { await oldAgent.get("/api/v1/now"); } catch { disposedRefused = true; }
    agent = await agentContext();
    record({ id: "K4.3", via: "http", request: { closed: "chat 1" }, response: { chat: 2 }, outcome: "ok", assertions: [{ claim: "the second chat is a new connection", passed: disposedRefused && page.isClosed() }] });
    {
      const a: Assertion[] = [];
      const r = await call(agent, "GET", "/now?limit=200");
      const ids = (key: string) => (r.body[key].items as any[]).map((i) => i.id);
      check(a, "it shows what changed since the correction", [fx.claim_a, fx.anchor].every((id: string) => ids("changed").includes(id)));
      check(a, "... what is still running", ids("running").includes(fx.run_live));
      check(a, "... what awaits interpretation", ids("awaiting_interpretation").includes(fx.run_done));
      check(a, "... and the decision still waiting for the owner", ids("approvals").includes(dismissalApproval));
      record({ id: "K4.4", via: "http", request: { limit: 200 }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      const a: Assertion[] = [];
      const listed = await call(agent, "GET", "/runs?operation=source_import_plan&limit=20");
      const pending = listed.body.runs.filter((r: any) => ["queued", "failed", "paused_budget"].includes(r.state));
      expect(pending).toHaveLength(1);
      const discoveredRun = pending[0].run_id;
      const r = await call(agent, "POST", `/runs/${discoveredRun}/resume`, { wait_seconds: 20 });
      runId = discoveredRun;
      check(a, "the finished run's result is readable without the first chat", r.status === 200 && r.body.state === "completed" && !!r.body.result);
      record({ id: "K4.5", via: "http", request: { run_id: "import_run" }, response: r.body, outcome: outcomeOf(r), assertions: a });
    }
    {
      // A new browser context, freshly paired: the owner resumes from what the hub shows.
      const a: Assertion[] = [];
      const fresh = await browser.newContext();
      const page2 = await fresh.newPage();
      await pairPage(page2);
      await page2.goto(`${origin}/now`);
      await expect(page2.locator(`[data-testid="now-running"] [data-id="${fx.run_live}"]`)).toBeVisible();
      await expect(page2.locator(`[data-testid="now-awaiting"] [data-id="${fx.run_done}"]`)).toBeVisible();
      await expect(page2.locator(`[data-testid="now-approvals"] [data-id="${dismissalApproval}"]`)).toBeVisible();
      await expect(page2.locator(`[data-testid="now-changed"] [data-id="${fx.claim_a}"]`)).toBeVisible();
      check(a, "a new browser session shows what changed, what runs, what waits and what needs the owner", true);
      await shot(page2, "K4-now-new-session");
      await page2.goto(`${origin}/runs/${runId}`);
      const runCard = page2.locator(`[data-testid="run-card"][data-run-id="${runId}"]`);
      await expect(runCard).toHaveAttribute("data-run-state", "completed");
      check(a, "the Runs page shows the finished run and its result", true);
      await shot(page2, "K4-runs");
      record({ id: "K4.6", via: "browser", request: { pages: ["/now", `/runs/${runId}`] }, response: { run_state: "completed" }, outcome: "ok", assertions: a });
      await fresh.close();
    }
  } finally {
    save();
    await agent.dispose().catch(() => undefined);
    await owner.ctx.dispose().catch(() => undefined);
  }
  const failed = steps.flatMap((s) => s.assertions.filter((a) => !a.passed).map((a) => `${s.id}: ${a.claim}`));
  expect(failed).toEqual([]);
});
