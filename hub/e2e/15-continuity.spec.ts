// CP10.4 research continuity in the real hub (decision 0018). A local agent (bearer token, agent
// scope) registers external runs and proposes a correction; the owner works in the browser. Every
// outcome is checked against stored state through a separately paired owner client:
//   - Now lists running, awaiting-interpretation and approval items, each with its reason; a
//     dismissal is a reviewed entry that cites the run and gives the reason, and the run leaves
//     the queue only then;
//   - an applied correction that supersedes an assumption flags the dependent claim, action's
//     claim chain and manuscript anchor in the same commit, and an agent's apply is refused
//     with approval_required until the owner decides;
//   - Artifacts shows a reference-only file as never backed up, and unavailable once removed.
import AxeBuilder from "@axe-core/playwright";
import { request as pwRequest, type APIRequestContext } from "@playwright/test";
import { existsSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, origin, test, type Api, type HubInfo } from "./fixtures";

type AgentHub = HubInfo & { agent_token: string; work: string };

async function agentContext(hub: AgentHub): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${hub.agent_token}` },
  });
}

async function agentPost<T = any>(agent: APIRequestContext, path: string, body: unknown, status = 200): Promise<T> {
  const reply = await agent.post(`/api/v1${path}`, { data: body });
  expect(reply.status(), `${path}: ${await reply.text()}`).toBe(status);
  return (await reply.json()) as T;
}

function entryBody(title: string, text: string, category = "note", extra: Record<string, unknown> = {}) {
  return {
    title,
    text,
    category,
    evidence: [],
    claim_refs: [],
    author_ref: null,
    origin: "typed",
    quantities: [],
    review: "unreviewed",
    ...extra,
  };
}

function create(kind: string, id: string, body: unknown) {
  return { op: "create", kind, id, base_rev: 0, sensitivity: "normal", occurred_at: null, body };
}

async function ownerApply(api: Api, operations: unknown[], protectedRequest = false) {
  const request = {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations,
  };
  if (!protectedRequest) return api.post("/transactions", request);
  const preview = await api.post<{ digest: string }>("/decisions/preview", { request });
  return api.post("/decisions/apply", { request, digest: preview.digest });
}

test("Now: running, awaiting and approvals with reasons; a dismissal is a reviewed entry", async ({
  paired: page,
  hub,
  api,
}) => {
  const agentHub = hub as AgentHub;
  const agent = await agentContext(agentHub);
  try {
    const running = await agentPost(agent, "/executions", {
      title: "E2E live sweep",
      host_label: "lab workstation",
      reporter: "e2e agent",
      command_summary: "simulate --regime e2e",
      working_root_alias: "research",
      expected_outputs: ["sweep.csv"],
      request_id: crypto.randomUUID(),
    });
    const runningId = running.results[0].id as string;
    await agentPost(agent, `/executions/${runningId}/report`, { state: "reported_running" });
    const finished = await agentPost(agent, "/executions", {
      title: "E2E finished sweep",
      host_label: "lab workstation",
      reporter: "e2e agent",
      request_id: crypto.randomUUID(),
    });
    const finishedId = finished.results[0].id as string;
    await agentPost(agent, `/executions/${finishedId}/report`, { state: "reported_completed" });

    // The agent asks for something protected (restricting a note): a pending approval.
    const noteId = `entry_${crypto.randomUUID()}`;
    await agentPost(agent, "/transactions", {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await api.seq(),
      operations: [create("entry", noteId, entryBody("Now e2e note", "Agent note.", "note", { origin: "agent" }))],
    });
    const asked = await agentPost<{ error: { code: string; details: { approval_request_id: string } } }>(
      agent,
      "/transactions",
      {
        schema_version: 1,
        project_id: hub.project_id,
        request_id: crypto.randomUUID(),
        expected_seq: await api.seq(),
        operations: [
          {
            op: "revise",
            id: noteId,
            base_rev: 1,
            sensitivity: "restricted",
            occurred_at: null,
            body: entryBody("Now e2e note", "Agent note.", "note", { origin: "agent" }),
          },
        ],
      },
      403,
    );
    expect(asked.error.code).toBe("approval_required");
    const approvalId = asked.error.details.approval_request_id;

    await page.goto(`${hub.url}now`);
    await expect(page.getByRole("heading", { level: 1, name: "Now" })).toBeVisible();
    const live = page.locator(`[data-testid="now-running"] [data-id="${runningId}"]`);
    await expect(live).toBeVisible();
    await expect(live.getByTestId("now-reason")).toContainText("Reported running");
    await expect(live.getByTestId("now-reason")).toContainText("never controlled");
    // The reason's report time is shown in the owner's zone, not as a raw UTC stamp (0048).
    await expect(live.getByTestId("now-reason")).toContainText(/Reported running at \w{3}, \d{1,2} \w{3} \d{4}, \d{2}:\d{2} \(.+, UTC[+-]\d{2}:\d{2}\)/);
    await expect(live.getByTestId("now-reason")).not.toContainText(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/);
    const waiting = page.locator(`[data-testid="now-awaiting"] [data-id="${finishedId}"]`);
    await expect(waiting).toBeVisible();
    await expect(waiting.getByTestId("now-reason")).toContainText("no assessment or reviewed entry cites it");
    const approval = page.locator(`[data-testid="now-approvals"] [data-id="${approvalId}"]`);
    await expect(approval).toBeVisible();
    await expect(approval.getByTestId("now-reason")).toContainText("asked for your decision");
    // The same facts through the API the browser used.
    const view = await api.get<any>("/now");
    expect(view.running.items.map((i: any) => i.id)).toContain(runningId);
    expect(view.awaiting_interpretation.items.map((i: any) => i.id)).toContain(finishedId);
    expect(view.approvals.items.map((i: any) => i.id)).toContain(approvalId);
    expect(view.running.items.map((i: any) => i.id)).not.toContain(finishedId);

    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);

    // The owner dismisses the finished run with a reason: a reviewed entry that cites it.
    const reason = "E2E: this sweep used the old drag model; nothing from it is used.";
    await waiting.getByText("Record an interpretation or dismiss with a reason").click();
    await waiting.getByTestId("resolve-text").fill(reason);
    await waiting.getByTestId("resolve-dismissed").click();
    await expect(waiting).toHaveCount(0);
    const queue = await api.get<{ items: { target_id: string }[] }>("/awaiting-interpretation");
    expect(queue.items.map((i) => i.target_id)).not.toContain(finishedId);
    const entries = await api.records<any>("entry");
    const dismissal = entries.find((e) => e.body.title === "Dismissed: E2E finished sweep");
    expect(dismissal, "the dismissal is a stored entry").toBeTruthy();
    expect(dismissal!.body).toMatchObject({ review: "reviewed", category: "decision", text: reason });
    const links = await api.get<{ relations: { source: string; relation: string; target: string }[] }>(
      `/knowledge/links/${finishedId}`,
    );
    expect(links.relations).toContainEqual({ source: dismissal!.id, relation: "cites", target: finishedId });
    // Review F8: every registered run stays visible, whatever became of it. A cancelled run and
    // the interpreted one are listed with their state and what interpreted them.
    const cancelled = await agentPost(agent, "/executions", {
      title: "E2E cancelled sweep",
      host_label: "lab workstation",
      reporter: "e2e agent",
      request_id: crypto.randomUUID(),
    });
    const cancelledId = cancelled.results[0].id as string;
    await agentPost(agent, `/executions/${cancelledId}/report`, { state: "reported_cancelled" });
    await page.reload();
    await page.getByTestId("now-executions").getByText(/All registered runs/).click();
    const interpretedRow = page.locator(`[data-testid="execution-row"][data-id="${finishedId}"]`);
    await expect(interpretedRow).toHaveAttribute("data-state", "reported_completed");
    await expect(interpretedRow).toContainText("Interpreted by a reviewed entry");
    await expect(page.locator(`[data-testid="execution-row"][data-id="${cancelledId}"]`)).toHaveAttribute(
      "data-state",
      "reported_cancelled",
    );
    const listed = await api.get<{ executions: { id: string; effective_state: string }[] }>("/executions");
    expect(listed.executions.find((e) => e.id === cancelledId)?.effective_state).toBe("reported_cancelled");
    // The still-running run is untouched, and the agent cannot dismiss anything itself.
    expect((await api.get<any>("/now")).running.items.map((i: any) => i.id)).toContain(runningId);
    const refused = await agentPost<any>(
      agent,
      `/awaiting-interpretation/${runningId}/resolve`,
      { resolution: "dismissed", text: "not awaiting", request_id: crypto.randomUUID() },
      400,
    );
    expect(refused.error.code).toBe("validation_error");
  } finally {
    await agent.dispose();
  }
});

test("an applied correction superseding an assumption flags dependents in one commit", async ({
  paired: page,
  hub,
  api,
}) => {
  const agentHub = hub as AgentHub;
  const agent = await agentContext(agentHub);
  try {
    const assumption = `entry_${crypto.randomUUID()}`;
    const replacement = `entry_${crypto.randomUUID()}`;
    const claim = `claim_${crypto.randomUUID()}`;
    const anchor = `anchor_${crypto.randomUUID()}`;
    const correction = `correction_${crypto.randomUUID()}`;
    const scope = {
      system: "e2e system",
      operating_regime: "e2e regime",
      comparator: "e2e comparator",
      model_fidelity: "e2e fidelity",
      validation_level: "none",
      objective: "E2E objective: replace the baseline?",
    };
    await ownerApply(api, [
      create("entry", assumption, entryBody("E2E constant drag", "Drag is constant.", "assumption")),
      create("entry", replacement, entryBody("E2E speed-dependent drag", "Drag grows with speed squared.", "assumption")),
      create("claim", claim, {
        statement: "E2E claim relying on constant drag.",
        scope,
        evidence: [],
        assessment_refs: [],
        state: "proposed",
        superseded_by: null,
      }),
      create("anchor", anchor, {
        paper_id: hub.seed.paper_id,
        manuscript_version_id: hub.seed.manuscript_version_id,
        locator: { type: "text", start_line: 1, end_line: 1 },
        claim_refs: [],
        evidence: [],
        status: "current",
        reason: null,
      }),
      {
        op: "relation_add",
        source_ref: { id: claim, rev: 1 },
        target_ref: { id: assumption, rev: 1 },
        relation: "depends_on",
      },
      {
        op: "relation_add",
        source_ref: { id: assumption, rev: 1 },
        target_ref: { id: anchor, rev: 1 },
        relation: "used_in",
      },
    ]);
    // The claim's page shows its objective and the assumption it relies on, before any change.
    await page.goto(`${hub.url}knowledge`);
    const card = page.locator(`[data-testid="claim-card"][id="${claim}"]`);
    await expect(card).toContainText("E2E objective: replace the baseline?");
    await expect(card.getByTestId("claim-assumptions")).toContainText("E2E constant drag");
    await expect(card.getByTestId("claim-flag")).toHaveCount(0);

    // The agent proposes the correction and tries to apply it: approval_required.
    await agentPost(agent, "/transactions", {
      schema_version: 1,
      project_id: hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await api.seq(),
      operations: [
        create("correction", correction, {
          target_refs: [{ id: assumption, rev: 1 }],
          replacement_refs: [{ id: replacement, rev: 1 }],
          reason: "E2E: constant drag fails at speed.",
          evidence: [],
          effects: [
            {
              target_ref: { id: assumption, rev: 1 },
              effect: "supersede",
              replacement_ref: { id: replacement, rev: 1 },
              replacement_text: null,
            },
          ],
          status: "proposed",
          applied_seq: null,
        }),
      ],
    });
    const applyOps = [{ op: "correction_apply", id: correction, base_rev: 1 }];
    const denied = await agentPost<any>(
      agent,
      "/transactions",
      {
        schema_version: 1,
        project_id: hub.project_id,
        request_id: crypto.randomUUID(),
        expected_seq: await api.seq(),
        operations: applyOps,
      },
      403,
    );
    expect(denied.error.code).toBe("approval_required");
    expect((await api.record(assumption)).body.superseded_by).toBeNull();
    expect((await api.record(anchor)).body.status).toBe("current");

    // The owner applies it; effect, flags and anchor move share one project sequence.
    const applied = await ownerApply(api, applyOps, true);
    const commitSeq = applied.result.project_seq as number;
    expect((await api.record(assumption)).body.superseded_by).toEqual({ id: replacement, rev: 1 });
    const anchorNow = await api.record(anchor);
    expect(anchorNow.body.status).toBe("needs_review");
    expect(anchorNow.body.reason).toContain(correction);
    const view = await api.get<any>("/now?limit=200");
    const flagged = new Map<string, any>(view.changed.items.map((i: any) => [i.id, i]));
    for (const id of [claim, anchor]) {
      expect(flagged.has(id), `${id} is flagged`).toBe(true);
      expect(flagged.get(id).cause_id).toBe(correction);
      expect(flagged.get(id).seq).toBe(commitSeq);
    }
    expect(flagged.has(replacement)).toBe(false);
    expect(flagged.has(correction)).toBe(false);

    // The pages say so, with the reason.
    await page.goto(`${hub.url}knowledge`);
    await expect(card.getByTestId("claim-flag")).toContainText("Needs review");
    await expect(card.getByTestId("claim-assumptions").locator(`[data-assumption-id="${assumption}"]`)).toHaveAttribute(
      "data-assumption-state",
      "superseded",
    );
    await expect(page.locator(`[data-testid="assumption-card"][id="${assumption}"]`)).toHaveAttribute(
      "data-assumption-state",
      "superseded",
    );
    await page.goto(`${hub.url}now`);
    const changed = page.locator(`[data-testid="now-changed"] [data-id="${claim}"]`);
    await expect(changed).toBeVisible();
    await expect(changed.getByTestId("now-reason")).toContainText("Needs review");
  } finally {
    await agent.dispose();
  }
});

test("Artifacts: reference-only bytes are never backed up; a removed file is unavailable", async ({
  paired: page,
  hub,
  api,
}) => {
  const agentHub = hub as AgentHub;
  const file = join(agentHub.work, "research", "e2e-reference-notes.txt");
  writeFileSync(file, "reference-only e2e notes\nline two\n");
  try {
    // CP10.3 (decision 0017): an import plan and its commit answer an operation run; wait for
    // each and read the plan from the run's result (decision 0019).
    type Plan = { plan_id: string; items: { source_id: string; version_id: string }[] };
    const planRun = await api.post<{ state: string; result: Plan | null }>("/sources/plans", {
      root_alias: "research",
      paths: ["e2e-reference-notes.txt"],
      retention: "reference",
      wait_seconds: 20,
    });
    expect(planRun.state).toBe("completed");
    const plan = planRun.result!;
    const commitRun = await api.post<{ state: string }>(`/sources/plans/${plan.plan_id}/commit`, {
      wait_seconds: 20,
    });
    expect(commitRun.state).toBe("completed");
    const versionId = plan.items[0]!.version_id;

    await page.goto(`${hub.url}artifacts`);
    await expect(page.getByRole("heading", { level: 1, name: "Artifacts" })).toBeVisible();
    const card = page.locator(`[data-testid="artifact-card"][data-version-id="${versionId}"]`);
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute("data-availability", "reference_only");
    await expect(card).toHaveAttribute("data-backup", "not_backed_up");
    await expect(card.getByTestId("backup-status")).toHaveText("Not backed up");
    await expect(page.getByTestId("coverage-totals")).toContainText("Whole project");
    await expect(card).toContainText("never backed up");
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    const before = await api.get<any>("/artifacts/coverage?limit=200");
    const stored = before.items.find((i: any) => i.version_id === versionId);
    expect(stored).toMatchObject({ availability: "reference_only", storage: "external" });
    expect(stored.backup.covered).toBe(false);

    // Remove the file; the page shows it unavailable and still not backed up.
    unlinkSync(file);
    expect(existsSync(file)).toBe(false);
    await card.getByTestId("check-now").click();
    await expect(card.getByTestId("check-result")).toContainText("not reachable");
    await page.reload();
    const gone = page.locator(`[data-testid="artifact-card"][data-version-id="${versionId}"]`);
    await expect(gone).toHaveAttribute("data-availability", "unavailable");
    await expect(gone).toHaveAttribute("data-backup", "not_backed_up");
    await expect(gone.getByTestId("availability")).toHaveText("Unavailable");
    const after = await api.get<any>("/artifacts/coverage?limit=200");
    const afterItem = after.items.find((i: any) => i.version_id === versionId);
    expect(afterItem).toMatchObject({ availability: "unavailable" });
    expect(afterItem.backup.covered).toBe(false);
    // Retained sources are unaffected by the removal.
    expect(after.totals.retained).toBeGreaterThan(0);
  } finally {
    if (existsSync(file)) unlinkSync(file);
  }
});
