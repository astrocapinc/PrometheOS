// CP6.4 against the real hub: a visible recording (synthetic tone device), recovery of a
// cancelled one, transcription with the labelled synthetic engine, playback, a correction that
// keeps the old version, a meeting from the transcript with audio-cited items, explicit
// reconcile choices, and the optional AI provider with a local server and an outage. Every UI
// step is followed by a re-read of the stored state through the API.
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, nav, shot, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target.join(" ")) }));
}

const run = Date.now();
const TITLE = `Standup ${run}`;
const state: {
  sessionId?: string;
  audioVersion?: string;
  transcript?: string;
  corrected?: string;
  meetingId?: string;
  commitmentId?: string;
  similarId?: string;
  segments?: { id: string; start_ms: number; end_ms: number; text: string }[];
} = {};

test("a visible recording: devices, reminder, pause, resume, stop; the audio is registered", async ({
  paired: page,
  hub,
  api,
}) => {
  const problems: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) problems.push(m.text());
  });
  await page.goto(`${hub.url}capture?type=audio`);
  await expect(page.getByRole("heading", { level: 2, name: "Record a meeting" })).toBeVisible();
  const start = page.getByRole("button", { name: "Start recording" });
  await expect(start).toBeDisabled();
  await page.getByLabel("Recording title").fill(TITLE);
  await page.getByTestId("device-synthetic-mic").check();
  await page.getByLabel("Who can see this recording").selectOption("normal");
  await expect(start).toBeDisabled(); // the participant reminder is not acknowledged yet
  await expect(page.getByText("make sure every participant knows and has agreed")).toBeVisible();
  await page.getByTestId("consent").check();
  await expect(start).toBeEnabled();
  expect((await api.get("/recordings")).sessions).toHaveLength(0);
  await start.click();

  const active = page.getByTestId("recording-active");
  await expect(active).toContainText("Recording:");
  await expect(active).toHaveAttribute("data-state", "recording");
  // Visible on every page while it runs, with a way back.
  await nav(page, "Now").click();
  await expect(page.getByTestId("recording-banner")).toContainText("Recording in progress");
  await page.getByRole("link", { name: "Open the recorder" }).click();
  await expect(page.getByTestId("recording-active")).toBeVisible();
  await expect(page.getByTestId("recording-elapsed")).not.toHaveText("0:00", { timeout: 20_000 });
  await shot(page, "audio-recording-active");

  const live = (await api.get("/recordings")).sessions[0];
  state.sessionId = live.session_id;
  expect(live).toMatchObject({ state: "recording", title: TITLE, sensitivity: "normal" });
  expect(live.sources.map((s: any) => s.id)).toEqual(["synthetic-mic"]);

  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByTestId("recording-active")).toHaveAttribute("data-state", "paused");
  await expect(page.getByTestId("recording-active")).toContainText("Paused:");
  expect((await api.get(`/recordings/${state.sessionId}`)).state).toBe("paused");
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.getByTestId("recording-active")).toHaveAttribute("data-state", "recording");
  await expect(page.getByTestId("recording-elapsed")).not.toHaveText("0:00");
  await page.getByRole("button", { name: "Stop and save" }).click();
  await expect(page.getByTestId("recording-active")).toHaveCount(0, { timeout: 20_000 });
  await expect(page.getByTestId("recording-banner")).toHaveCount(0);

  // The recorder leaves the page when recording stops; the import that follows can still be
  // running on a loaded machine, so poll for the stored state (the expect timeout bounds it).
  await expect
    .poll(async () => (await api.get(`/recordings/${state.sessionId}`)).state, { message: "recording state" })
    .toBe("imported");
  const done = await api.get(`/recordings/${state.sessionId}`);
  expect(done.elapsed_ms).toBeGreaterThan(1500);
  state.audioVersion = done.version_id;
  const version = await api.record(done.version_id);
  expect(version.body).toMatchObject({ mime: "audio/wav", availability: "retained" });
  expect(version.sensitivity).toBe("normal");
  const row = page.locator(`[data-version="${done.version_id}"]`);
  await expect(row).toContainText(TITLE);
  expect(problems).toEqual([]);
});

test("a cancelled recording keeps its segments: recover saves a partial recording, discard removes the rest", async ({
  paired: page,
  hub,
  api,
}) => {
  const begin = async (title: string) => {
    await page.goto(`${hub.url}capture?type=audio`);
    await page.getByLabel("Recording title").fill(title);
    await page.getByTestId("device-synthetic-mic").check();
    await page.getByLabel("Who can see this recording").selectOption("normal");
    await page.getByTestId("consent").check();
    await page.getByRole("button", { name: "Start recording" }).click();
    await expect(page.getByTestId("recording-elapsed")).not.toHaveText("0:00", { timeout: 20_000 });
    await page.getByRole("button", { name: /Cancel \(keep segments\)/ }).click();
    await expect(page.getByTestId("recording-active")).toHaveCount(0, { timeout: 20_000 });
  };
  await begin(`Cancelled keep ${run}`);
  await begin(`Cancelled drop ${run}`);
  const waiting = page.getByTestId("recoverable-session");
  await expect(waiting).toHaveCount(2);
  const sessions = (await api.get("/recordings")).sessions.filter((s: any) => s.recoverable);
  expect(sessions.map((s: any) => s.state)).toEqual(["cancelled", "cancelled"]);

  const keep = waiting.filter({ hasText: `Cancelled keep ${run}` });
  await expect(keep).toContainText("Cancelled after");
  await keep.getByRole("button", { name: "Recover what was recorded" }).click();
  await expect(page.getByText(`“Cancelled keep ${run}” was saved as a partial recording.`)).toBeVisible();
  const drop = waiting.filter({ hasText: `Cancelled drop ${run}` });
  await drop.getByRole("button", { name: "Discard the segments" }).click();
  // Deleting is destructive: one more explicit step, and nothing is deleted before it.
  expect((await api.get("/recordings?include_done=true")).sessions.find((s: any) => s.title === `Cancelled drop ${run}`).state).toBe("cancelled");
  await drop.getByRole("button", { name: "Yes, delete these segments for good" }).click();
  await expect(page.getByTestId("recoverable-sessions")).toHaveCount(0);

  const all = (await api.get("/recordings?include_done=true")).sessions;
  expect(all.find((s: any) => s.title === `Cancelled keep ${run}`).state).toBe("imported");
  expect(all.find((s: any) => s.title === `Cancelled drop ${run}`).state).toBe("discarded");
  const source = (await api.records("source")).find((s) => s.body.title === `Cancelled keep ${run}`);
  expect(source?.body.description).toContain("Partial recording");
  expect((await api.records("source")).find((s) => s.body.title === `Cancelled drop ${run}`)).toBeUndefined();
});

test("transcribe on this computer (synthetic engine is labelled), with progress, then open the transcript", async ({
  paired: page,
  hub,
  api,
}) => {
  await page.goto(`${hub.url}capture?type=audio`);
  const row = page.locator(`[data-version="${state.audioVersion}"]`);
  await expect(row).toContainText("Not transcribed yet.");
  // The real speech model is not installed here: the plan says so and nothing is downloaded.
  await expect(page.getByText("Local speech model is not installed")).toBeVisible();
  await page.getByText("Local speech model is not installed").click();
  await expect(page.getByTestId("model-plan")).toContainText("MIT");
  await expect(page.getByTestId("model-plan")).toContainText("141");
  const synthetic = row.getByLabel("Engine");
  await expect(synthetic.locator("option", { hasText: "not a real transcript" })).toHaveCount(1);
  await synthetic.selectOption("synthetic");
  await row.getByRole("button", { name: "Transcribe on this computer" }).click();
  await expect(row.getByTestId("run-state")).toHaveAttribute("data-state", "completed", { timeout: 30_000 });
  await expect(row.getByTestId("run-state")).toContainText("finished");

  const runs = (await api.get(`/audio/transcriptions?version_id=${state.audioVersion}`)).runs;
  expect(runs).toHaveLength(1);
  state.transcript = runs[0].result_version_id;
  const view = await api.get(`/transcripts/${state.transcript}`);
  expect(view.transformation.engine).toBe("synthetic-engine");
  state.segments = view.payload.segments;
  expect(state.segments?.length).toBeGreaterThanOrEqual(2);
  await row.getByRole("link", { name: "Open the transcript" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Transcript" })).toBeVisible();
  await expect(page.getByTestId("transcript-segment")).toHaveCount(state.segments?.length ?? 0);
  await expect(page.getByTestId("audio-element")).toBeVisible();
});

test("playback is explicit and keyboard operable; a correction creates a new version and keeps the old one", async ({
  paired: page,
  hub,
  api,
}) => {
  const problems: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) problems.push(m.text());
  });
  await page.goto(`${hub.url}transcripts/${state.transcript}`);
  const audio = page.getByTestId("audio-element");
  await expect(audio).toBeVisible();
  expect(await audio.evaluate((a: HTMLAudioElement) => a.autoplay || !a.paused)).toBe(false);
  // The recording is served from the hub with byte ranges and a locked-down policy.
  const content = await api.ctx.get(`/api/v1/versions/${state.audioVersion}/content`, {
    headers: { Range: "bytes=0-99" },
  });
  expect(content.status()).toBe(206);
  expect(content.headers()["content-type"]).toBe("audio/wav");
  expect(content.headers()["accept-ranges"]).toBe("bytes");

  // Keyboard: Tab to a segment's Play button and press Enter; the audio seeks to its start.
  const second = page.getByTestId("transcript-segment").nth(1);
  const play = second.getByRole("button", { name: /^Play / });
  await play.focus();
  await expect(play).toBeFocused();
  await page.keyboard.press("Enter");
  const startSeconds = (state.segments?.[1]?.start_ms ?? 0) / 1000;
  await expect
    .poll(async () => audio.evaluate((a: HTMLAudioElement) => a.currentTime), { timeout: 10_000 })
    .toBeGreaterThanOrEqual(startSeconds);

  // Correct the first segment (a new version; the transcript page moves to it).
  const first = page.getByTestId("transcript-segment").first();
  await first.getByRole("button", { name: /^Correct/ }).click();
  const box = page.getByLabel(/^Corrected text for/);
  await expect(box).toBeFocused({ timeout: 2000 }).catch(() => undefined);
  await box.fill("Compare the controllers under actuator limits.");
  await page.getByRole("button", { name: "Save correction" }).click();
  await expect(page).not.toHaveURL(new RegExp(state.transcript ?? "no-transcript"));
  state.corrected = page.url().split("/transcripts/")[1];
  await expect(page.getByTestId("transcript-segment").first()).toContainText("Compare the controllers under actuator limits.");
  const newer = await api.get(`/transcripts/${state.corrected}`);
  expect(newer.superseded_by_correction).toBe(false);
  expect(newer.payload.segments[0].text).toBe("Compare the controllers under actuator limits.");
  const older = await api.get(`/transcripts/${state.transcript}`);
  expect(older.superseded_by_correction).toBe(true);
  expect(older.latest_version_id).toBe(state.corrected);
  expect(older.payload.segments[0].text).toBe(state.segments?.[0]?.text); // untouched history
  await page.goto(`${hub.url}transcripts/${state.transcript}`);
  await expect(page.getByTestId("superseded-banner")).toContainText("corrected version");
  expect(problems).toEqual([]);
  await shot(page, "audio-transcript");
});

test("a meeting from the transcript: audio-cited items, evidence highlighting, quotes and corrections", async ({
  paired: page,
  hub,
  api,
}) => {
  await page.goto(`${hub.url}transcripts/${state.corrected}`);
  await page.getByLabel("Meeting title").fill(`Recorded ${TITLE}`);
  await page.getByRole("button", { name: "Create meeting" }).click();
  await expect(page.getByRole("heading", { level: 1, name: `Recorded ${TITLE}` })).toBeVisible();
  await expect(page.getByText("No items proposed yet.", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Recording transcript (original)" })).toBeVisible();
  const meeting = (await api.records("meeting")).find((m) => m.body.title === `Recorded ${TITLE}`);
  expect(meeting).toBeTruthy();
  state.meetingId = meeting?.id;
  expect(meeting?.body.source_version_refs.map((r: any) => r.id)).toEqual([state.corrected, state.audioVersion]);

  // An external agent proposes items through the same task gate, citing audio spans.
  const view = await api.get(`/transcripts/${state.corrected}`);
  const [s1, s2] = view.payload.segments;
  const cite = (s: any) => ({
    source_id: view.source_id,
    version_id: state.corrected,
    locator: { type: "audio", start_ms: s.start_ms, end_ms: s.end_ms },
    quote_sha256: null,
  });
  const task = await api.post("/tasks/prepare", { purpose: "meeting_extract", meeting_id: state.meetingId });
  expect(task.context).toContain(`${s1.id} [`);
  await api.post(`/tasks/${task.task_id}/submit`, {
    request_id: crypto.randomUUID(),
    result: {
      purpose: "meeting_extract",
      items: [
        { type: "commitment", text: "Compare the controllers under actuator limits", evidence: [cite(s1)], proposed_owner: null, due_at: null },
        { type: "question", text: "Which regime was meant here?", evidence: [cite(s2)], proposed_owner: null, due_at: null },
      ],
    },
  });
  await page.reload();
  await expect(page.getByTestId("meeting-item")).toHaveCount(2);
  const commitment = page.getByTestId("meeting-item").filter({ hasText: "Compare the controllers under actuator limits" });
  state.commitmentId = (await commitment.getAttribute("data-item-id")) ?? undefined;
  await expect(commitment.getByTestId("item-classification")).toContainText("No similar action found");
  await expect(commitment.getByTestId("audio-quote")).toContainText("Compare the controllers under actuator limits.");

  await commitment.getByRole("button", { name: /Show evidence in the transcript/ }).click();
  const cited = page.getByTestId("original-pane").locator("li.cited");
  await expect(cited).toHaveCount(1);
  await expect(cited).toHaveAttribute("data-segment", s1.id);
  await expect(page.getByTestId("play-selected-span")).toBeVisible();
  await shot(page, "audio-meeting-reconcile");

  // Adopt it: the same two-step owner decision as for typed notes.
  await commitment.getByRole("button", { name: "Adopt as action" }).click();
  await commitment.getByRole("button", { name: "Approve and create action" }).click();
  await expect(commitment.getByTestId("adopted-notice")).toBeVisible();
  const actions = (await api.records("action")).filter((a) => a.body.commitment_item_id === state.commitmentId);
  expect(actions).toHaveLength(1);
  expect(actions[0]?.body.status).toBe("ready");
  expect((await api.record(state.commitmentId ?? "")).body.status).toBe("adopted");

  // The transcript is corrected again: the item still cites the version it was made from and
  // says that its words changed.
  await api.post(`/transcripts/${state.corrected}/corrections`, {
    edits: [{ segment_id: s1.id, text: "Compare both controllers under actuator limits." }],
  });
  await page.reload();
  const again = page.getByTestId("meeting-item").filter({ hasText: "Compare the controllers under actuator limits" });
  await expect(again.getByTestId("audio-text-changed")).toContainText("cited words changed");
  await expect(page.getByTestId("transcript-corrected")).toContainText("corrected version");
});

test("similar work is shown, never merged: 'already covered' is an explicit, recorded dismissal", async ({
  paired: page,
  hub,
  api,
}) => {
  const view = await api.get(`/transcripts/${state.corrected}`);
  // The transcript has few segments: cite a different (shorter) span of the second one, so the
  // exact locator differs from the question's.
  const s2 = view.payload.segments[1];
  const s3 = { start_ms: s2.start_ms, end_ms: s2.end_ms - 500 };
  const task = await api.post("/tasks/prepare", { purpose: "meeting_extract", meeting_id: state.meetingId });
  await api.post(`/tasks/${task.task_id}/submit`, {
    request_id: crypto.randomUUID(),
    result: {
      purpose: "meeting_extract",
      items: [
        {
          type: "suggestion",
          text: "Compare the controllers under actuator limits",
          evidence: [
            {
              source_id: view.source_id,
              version_id: state.corrected,
              locator: { type: "audio", start_ms: s3.start_ms, end_ms: s3.end_ms },
              quote_sha256: null,
            },
          ],
          proposed_owner: null,
          due_at: null,
        },
      ],
    },
  });
  await page.goto(`${hub.url}meetings/${state.meetingId}`);
  const similar = page
    .getByTestId("meeting-item")
    .filter({ hasText: "Suggestion:" })
    .filter({ hasText: "Compare the controllers under actuator limits" });
  await expect(similar).toHaveCount(1);
  state.similarId = (await similar.getAttribute("data-item-id")) ?? undefined;
  await expect(similar.getByTestId("item-classification")).toContainText("may overlap work already planned");
  await expect(similar.getByTestId("related-action")).toHaveCount(1);
  await expect(similar.getByTestId("related-action")).toContainText("shared wording");
  // Adopting anyway is available and says what stays distinct.
  await expect(similar.getByRole("button", { name: "Adopt as a separate action anyway" })).toBeVisible();

  await similar.getByRole("button", { name: /Treat as already covered by/ }).click();
  const form = similar.getByTestId("dismiss-form");
  await expect(form.getByLabel(/Why is this item being dismissed/)).toHaveValue(/Already covered by the action/);
  expect((await api.record(state.similarId ?? "")).body.status).toBe("proposed"); // nothing yet
  await form.getByRole("button", { name: "Dismiss this item" }).click();
  await expect(similar.getByTestId("dismissed-notice")).toBeVisible();
  const stored = await api.record(state.similarId ?? "");
  expect(stored.body.status).toBe("dismissed");
  const history = await api.get(`/records/${state.similarId}/history`);
  expect(JSON.stringify(history.transitions)).toContain("Already covered by the action");
  // No second action was created and the first is untouched.
  expect((await api.records("action")).filter((a) => a.body.title.includes("Compare the controllers"))).toHaveLength(1);
});

let local: Server | undefined;
const requests: any[] = [];
let reply = "";

test("optional AI provider: off by default, then a local server with a preview, a bounded run and a visible ledger", async ({
  paired: page,
  hub,
  api,
}) => {
  await page.goto(`${hub.url}ai`);
  await expect(page.getByRole("heading", { level: 1, name: "AI provider" })).toBeVisible();
  await expect(page.getByTestId("provider-policy")).toContainText("Off (no AI)");
  await expect(page.getByText("work without it", { exact: false })).toBeVisible();
  await expect(page.getByTestId("capability-table")).toContainText("local-whisper");
  // A meeting still works: the provider panel says AI is off and nothing needs it.
  const typed = await api.post("/meetings", {
    title: `Typed for AI ${run}`,
    text: "Supervisor: Compare the controllers with actuator limits.\nResearcher: I will send the plant result.\n",
    request_id: crypto.randomUUID(),
  });
  await page.goto(`${hub.url}meetings/${typed.meeting_id}`);
  await expect(page.getByTestId("provider-off")).toContainText("AI is off for this project");

  // A local model server on loopback (a real HTTP server, no network).
  local = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      requests.push({ url: req.url, auth: req.headers.authorization ?? null, body: JSON.parse(Buffer.concat(chunks).toString()) });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(reply);
    });
  });
  await new Promise<void>((resolve) => local?.listen(0, "127.0.0.1", resolve));
  const endpoint = `http://127.0.0.1:${(local?.address() as AddressInfo).port}/v1`;
  reply = JSON.stringify({
    id: "chatcmpl-local",
    model: "local-test",
    choices: [
      {
        index: 0,
        finish_reason: "stop",
        message: {
          role: "assistant",
          content: JSON.stringify({
            purpose: "meeting_extract",
            items: [
              {
                type: "suggestion",
                text: "Compare the controllers with actuator limits.",
                evidence: [
                  {
                    source_id: typed.source_id,
                    version_id: typed.version_id,
                    locator: { type: "text", start_line: 1, end_line: 1 },
                    quote_sha256: null,
                  },
                ],
                proposed_owner: null,
                due_at: null,
              },
            ],
          }),
        },
      },
    ],
    usage: { prompt_tokens: 900, completion_tokens: 80 },
  });

  await page.goto(`${hub.url}ai`);
  await page.getByLabel("Where model calls may go").selectOption("local-only");
  await page.getByLabel("Endpoint").fill(endpoint);
  await page.getByLabel("Model", { exact: true }).fill("local-test");
  await page.getByRole("button", { name: "Review the change" }).click();
  const review = page.getByTestId("config-review");
  await expect(review).toContainText("Local only");
  await expect(review).toContainText("route local");
  expect((await api.get("/provider")).revision).toBe(0); // nothing saved before the owner confirms
  await review.getByRole("button", { name: "Save these settings" }).click();
  await expect(page.getByTestId("config-applied")).toBeVisible();
  await expect(page.getByTestId("provider-route")).toHaveText("local");
  const status = await api.get("/provider");
  expect(status).toMatchObject({ policy: "local-only", route: "local", revision: 1 });
  expect(JSON.stringify(status)).not.toMatch(/secret|sk-/i);

  await page.goto(`${hub.url}meetings/${typed.meeting_id}`);
  await page.getByRole("button", { name: "Preview what would be sent" }).click();
  const preview = page.getByTestId("provider-preview");
  await expect(preview).toContainText("Ready to send");
  await expect(preview).toContainText("127.0.0.1");
  await expect(preview).toContainText("not included");
  await preview.getByText("Exactly what would be sent").click();
  await expect(page.getByTestId("provider-excerpt")).toContainText("Compare the controllers with actuator limits");
  expect(requests).toHaveLength(0); // a preview sends nothing
  expect((await api.records("item")).filter((i) => i.body.meeting_id === typed.meeting_id)).toHaveLength(0);
  await shot(page, "provider-preview");
  await preview.getByRole("button", { name: "Send this request" }).click();
  await expect(page.getByTestId("provider-result")).toContainText("added as items");
  expect(requests).toHaveLength(1);
  expect(requests[0].auth).toBeNull(); // a local server needs no credential
  expect(requests[0].body.response_format.type).toBe("json_schema");
  await expect(page.getByTestId("meeting-item")).toHaveCount(1);
  const items = (await api.records("item")).filter((i) => i.body.meeting_id === typed.meeting_id);
  expect(items).toHaveLength(1);
  expect(items[0]?.body.status).toBe("proposed"); // never adopted by a model
  const calls = (await api.get("/provider/calls")).calls;
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ state: "succeeded", route: "local" });
  await page.goto(`${hub.url}ai`);
  await expect(page.getByTestId("provider-calls")).toContainText("succeeded");
});

test("a provider outage shows what happened and leaves the model-free path untouched", async ({
  paired: page,
  hub,
  api,
}) => {
  const typed = await api.post("/meetings", {
    title: `Outage ${run}`,
    text: "Supervisor: Please check the venue limits.\n",
    request_id: crypto.randomUUID(),
  });
  await page.goto(`${hub.url}meetings/${typed.meeting_id}`);
  await page.getByRole("button", { name: "Preview what would be sent" }).click();
  const preview = page.getByTestId("provider-preview");
  await expect(preview).toContainText("Ready to send");
  await new Promise<void>((resolve) => {
    local?.close(() => resolve());
    local?.closeAllConnections();
  });
  const before = requests.length;
  await preview.getByRole("button", { name: "Send this request" }).click();
  const failure = page.getByTestId("provider-failure");
  await expect(failure).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("provider-fallback")).toContainText("task_");
  await expect(page.getByTestId("provider-fallback")).toContainText("does not depend on this provider");
  expect(requests).toHaveLength(before);
  expect((await api.records("item")).filter((i) => i.body.meeting_id === typed.meeting_id)).toHaveLength(0);
  const task = (await api.records("task")).filter((t) => t.body.status === "prepared").pop();
  expect(task).toBeTruthy(); // the same work stays open for an external agent
  const calls = (await api.get("/provider/calls")).calls;
  expect(calls[0]).toMatchObject({ state: "failed", actual_cost_usd: "0.000000" });
  await shot(page, "provider-outage");
  // Everything else still works.
  await nav(page, "Notebook").click();
  await expect(page.getByRole("heading", { level: 1, name: "Notebook" })).toBeVisible();
  await page.goto(`${hub.url}capture`);
  await page.getByLabel("Title", { exact: true }).fill(`After outage ${run}`);
  await page.getByLabel("Note", { exact: true }).fill("Typed while the provider was unreachable.");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("capture-saved")).toContainText(`After outage ${run}`);
});

for (const [name, path] of [
  ["recorder and import", "capture?type=audio"],
  ["AI provider", "ai"],
] as const) {
  test(`${name} has no serious or critical axe violations`, async ({ paired: page, hub }) => {
    await page.goto(`${hub.url}${path}`);
    await expect(page.locator(".state-loading")).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 2 }).first()).toBeVisible();
    expect(await seriousViolations(page)).toEqual([]);
  });
}

test("transcript page and a meeting from audio have no serious or critical axe violations", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}transcripts/${state.corrected}`);
  await expect(page.getByTestId("transcript-segment").first()).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
  await page.goto(`${hub.url}meetings/${state.meetingId}`);
  await expect(page.getByTestId("meeting-item").first()).toBeVisible();
  await page.getByTestId("meeting-item").first().getByRole("button", { name: /Show evidence in the transcript/ }).click();
  expect(await seriousViolations(page)).toEqual([]);
});

test("the recorder at phone width has no horizontal scrolling", async ({ paired: page, hub }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto(`${hub.url}capture?type=audio`);
  await expect(page.getByRole("heading", { level: 2, name: "Record a meeting" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.goto(`${hub.url}transcripts/${state.corrected}`);
  await expect(page.getByTestId("transcript-segment").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
});
