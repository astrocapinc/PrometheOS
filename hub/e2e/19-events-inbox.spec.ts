// A05, CP18.2-CP18.3 (C18-EVENTS, C18-QUEUE; decision 0035): the event inbox on Now. Work items
// due in 1, 6 and 13 days become deadline events when the checks run; the owner sees each with
// its reason and times in the owner's zone, acknowledges one with a note and the rest at once. A
// local agent (bearer token) reading the queue changes nothing, and its acknowledgement is an
// approval request. Every outcome is checked against stored state through the API.
import { request as pwRequest, type APIRequestContext } from "@playwright/test";
import { expect, origin, shot, test, type Api, type HubInfo } from "./fixtures";

test.use({ timezoneId: "Asia/Riyadh" });

type AgentHub = HubInfo & { agent_token: string };

async function agentContext(hub: AgentHub): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: origin(hub),
    extraHTTPHeaders: { Authorization: `Bearer ${hub.agent_token}` },
  });
}

function isoIn(days: number): string {
  return new Date(Date.now() + days * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

async function addItems(api: Api, stamp: number): Promise<Record<string, string>> {
  const ids: Record<string, string> = {};
  const operations = [1, 6, 13].map((days) => {
    const id = `action_${crypto.randomUUID()}`;
    ids[String(days)] = id;
    return {
      op: "create",
      kind: "action",
      id,
      base_rev: 0,
      sensitivity: "normal",
      occurred_at: null,
      body: {
        // One title carries markup: the inbox must show it as text (review s2-a F12).
        title: `Inbox item due in ${days} days ${stamp}${days === 13 ? " <b>bold</b>" : ""}`,
        reason: "Inbox e2e",
        source_refs: [],
        owner_ref: null,
        due_at: isoIn(days),
        dependencies: [],
        status: "proposed",
        blocker: null,
        completion_evidence: [],
        commitment_item_id: null,
        description: null,
        labels: [],
      },
    };
  });
  await api.post("/transactions", {
    schema_version: 1,
    project_id: api.hub.project_id,
    request_id: crypto.randomUUID(),
    expected_seq: await api.seq(),
    operations,
  });
  return ids;
}

type Ev = {
  event_id: string;
  type: string;
  subject_ref: { id: string };
  data: { days_before?: number };
  acknowledged_note: string | null;
};

test("deadline events appear in Now's inbox and the owner acknowledges them", async ({ paired: page, hub, api }) => {
  const ids = await addItems(api, Date.now());
  await page.goto(`${hub.url}now`);
  const inbox = page.getByTestId("event-inbox");
  await expect(inbox.getByRole("heading", { name: "Inbox" })).toBeVisible();
  await inbox.getByTestId("inbox-check").click();
  for (const [days, notice] of [["1", 2], ["6", 7], ["13", 14]] as const) {
    const row = inbox.locator(`[data-testid="inbox-event"][data-subject="${ids[days]}"]`);
    await expect(row).toHaveAttribute("data-type", "deadline.approaching");
    await expect(row.getByTestId("inbox-reason")).toContainText(`deadline is within ${notice} days`);
    await expect(row.getByTestId("inbox-when")).toContainText("(Asia/Riyadh, UTC+03:00)");
  }
  const marked = inbox.locator(`[data-testid="inbox-event"][data-subject="${ids["13"]}"]`);
  await expect(marked.getByTestId("inbox-subject")).toContainText("<b>bold</b>");
  await expect(marked.locator("b")).toHaveCount(0);
  // The owner's own new items raised no open "work item added" event.
  const own = (await api.get<{ items: Ev[] }>("/events?state=open&limit=200")).items.filter(
    (e) => e.type === "work_item.created" && Object.values(ids).includes(e.subject_ref.id),
  );
  expect(own).toHaveLength(0);
  const stored = (await api.get<{ items: Ev[] }>("/events?type=deadline.approaching&limit=200")).items;
  const notices = Object.fromEntries(
    stored.filter((e) => Object.values(ids).includes(e.subject_ref.id)).map((e) => [e.subject_ref.id, e.data.days_before]),
  );
  expect(notices).toEqual({ [ids["1"]]: 2, [ids["6"]]: 7, [ids["13"]]: 14 });
  await shot(page, "events-inbox");

  // Acknowledge one with a note: it leaves the inbox and keeps the note.
  const first = inbox.locator(`[data-testid="inbox-event"][data-subject="${ids["1"]}"]`);
  await first.getByTestId("inbox-note").fill("Slides are ready; presenting Thursday.");
  await first.getByTestId("inbox-ack").click();
  await expect(first).toHaveCount(0);
  const acked = (
    await api.get<{ items: Ev[] }>(`/events?state=acknowledged&type=deadline.approaching&subject_id=${ids["1"]}`)
  ).items;
  expect(acked.map((e) => [e.type, e.acknowledged_note])).toEqual([
    ["deadline.approaching", "Slides are ready; presenting Thursday."],
  ]);
  // A second check finds nothing new: each notice fires once.
  await inbox.getByTestId("inbox-check").click();
  await expect(page.getByRole("status").filter({ hasText: /Checks ran/ })).toHaveCount(1);
  const again = (await api.get<{ items: Ev[] }>(`/events?type=deadline.approaching&state=all&limit=200`)).items;
  expect(again.filter((e) => Object.values(ids).includes(e.subject_ref.id))).toHaveLength(3);
});

test("an agent's queue read changes nothing and its acknowledgement waits for the owner", async ({
  paired: page,
  hub,
  api,
}) => {
  const ids = await addItems(api, Date.now());
  await api.post("/jobs/tick", {});
  const agent = await agentContext(hub as AgentHub);
  const before = await api.seq();
  const queue = await agent.get("/api/v1/queue");
  expect(queue.status()).toBe(200);
  const entries = (await queue.json()).entries as { ref: string; entry: string; type?: string; subject_ref?: { id: string } }[];
  const notice = entries.find((e) => e.entry === "event" && e.subject_ref?.id === ids["6"]);
  expect(notice?.type).toBe("deadline.approaching");
  expect(entries.findIndex((e) => e.ref === ids["6"])).toBeLessThan(entries.findIndex((e) => e.ref === notice!.ref));
  expect(await api.seq()).toBe(before);

  const asked = await agent.post("/api/v1/events/acknowledge", { data: { event_ids: [notice!.ref] } });
  expect(asked.status()).not.toBe(200);
  expect((await asked.json()).error.code).toBe("approval_required");
  const open = (await api.get<{ items: Ev[] }>("/events?state=open&limit=200")).items;
  expect(open.map((e) => e.event_id)).toContain(notice!.ref);
  await agent.dispose();

  // The owner sees the request in the inbox, with a link to the exact request.
  await page.goto(`${hub.url}now`);
  const inbox = page.getByTestId("event-inbox");
  const request = inbox.locator('[data-testid="inbox-event"][data-type="approval.requested"]').last();
  await expect(request.getByTestId("inbox-reason")).toContainText("event_acknowledge");
  await expect(request.getByRole("link", { name: "Open the request" })).toBeVisible();

  // Acknowledge all shown: the inbox empties. Earlier specs on this shared hub leave events, so
  // the owner clears all but two through the API first (the button takes 50 at a time).
  let pending = (await api.get<{ items: Ev[] }>("/events?state=open&limit=200")).items.map((e) => e.event_id);
  while (pending.length > 2) {
    await api.post("/events/acknowledge", { event_ids: pending.slice(0, Math.min(50, pending.length - 2)) });
    pending = (await api.get<{ items: Ev[] }>("/events?state=open&limit=200")).items.map((e) => e.event_id);
  }
  await page.reload();
  await inbox.getByTestId("inbox-ack-all").click();
  await expect(inbox.getByTestId("inbox-events")).toHaveCount(0);
  await expect(inbox).toContainText("No unacknowledged events");
});
