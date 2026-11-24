// Capture of a typed note and of meeting notes, and the stale (409) path that keeps the draft.
import { expect, shot, test } from "./fixtures";

test("capture a typed note from Now; it is recorded and shown in the Notebook", async ({
  paired: page,
  hub,
  api,
}) => {
  const title = `Typed note ${Date.now()}`;
  await page.goto(`${hub.url}now`);
  await page.getByTestId("capture-cta").click();
  await expect(page.getByRole("heading", { level: 1, name: "Capture" })).toBeVisible();
  await page.getByLabel("Title", { exact: true }).fill(title);
  await page.getByLabel("Note", { exact: true }).fill("Energy recovery dropped with actuator limits on.\nSecond line.");
  await page.getByLabel("Kind of note").selectOption("finding");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("capture-saved")).toContainText(title);
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue("");

  const matches = (await api.records("entry")).filter((e) => e.body.title === title);
  expect(matches).toHaveLength(1);
  expect(matches[0]?.body).toMatchObject({
    category: "finding",
    origin: "typed",
    review: "reviewed",
    text: "Energy recovery dropped with actuator limits on.\nSecond line.",
  });

  await page.getByRole("link", { name: "View it in the Notebook" }).click();
  await expect(page.locator(`[id="${matches[0]?.id}"]`)).toContainText(title);
});

test("capture meeting notes; they are retained exactly and open as a meeting to review", async ({
  paired: page,
  hub,
  api,
}) => {
  const title = `Lab sync ${Date.now()}`;
  const notes = "Agenda: plant model\nAlex will rerun the constrained case.\nOpen: who checks units?\n";
  await page.goto(`${hub.url}capture`);
  await page.getByRole("radio", { name: "Meeting notes" }).check();
  await page.getByLabel("Meeting title").fill(title);
  await page.getByLabel("Notes", { exact: true }).fill(notes);
  await page.getByRole("button", { name: "Capture meeting notes" }).click();
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  await expect(page.getByText("No items proposed yet.", { exact: false })).toBeVisible();
  await expect(page.getByTestId("meeting-notes").locator("li")).toHaveCount(3);

  const meeting = (await api.records("meeting")).find((m) => m.body.title === title);
  expect(meeting).toBeTruthy();
  expect(page.url()).toBe(`${hub.url}meetings/${meeting?.id}`);
  const version = await api.get(`/versions/${meeting?.body.source_version_refs[0].id}/text`);
  expect(version.text).toBe(notes);
  await shot(page, "capture-meeting");
});

test("a stale write (409) keeps the draft and offers refresh and review; saving again succeeds once", async ({
  paired: page,
  hub,
  api,
}) => {
  const title = `Draft kept ${Date.now()}`;
  const text = "This draft must survive a conflict.";
  // Hold the change feed so this page does not learn about the next commit (as when the
  // long-poll is between requests), then move the project on through the API.
  await page.route("**/api/v1/changes**", () => {
    /* never answered */
  });
  await page.goto(`${hub.url}capture`);
  await expect(page.getByLabel("Title", { exact: true })).toBeVisible();
  await page.getByLabel("Title", { exact: true }).fill(title);
  await page.getByLabel("Note", { exact: true }).fill(text);
  await api.createNote(`Concurrent note ${Date.now()}`);

  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("stale-notice")).toContainText("changed since you loaded it");
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(title);
  await expect(page.getByLabel("Note", { exact: true })).toHaveValue(text);
  expect((await api.records("entry")).filter((e) => e.body.title === title)).toHaveLength(0);
  await shot(page, "capture-stale-409");

  await page.getByRole("button", { name: "Refresh and review" }).click();
  await expect(page.getByTestId("refreshed-notice")).toBeVisible();
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(title);
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("capture-saved")).toContainText(title);
  expect((await api.records("entry")).filter((e) => e.body.title === title)).toHaveLength(1);
});

test("Save note is disabled until the project has loaded, so an early click is never silently dropped", async ({
  paired: page,
  hub,
}) => {
  // The first headless-shell runs clicked "Save note" ~10 ms before GET /api/v1/project answered;
  // the form then did nothing at all. Hold that answer and prove the button waits for it.
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/project", async (route) => {
    await held;
    await route.continue();
  });
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/v1/transactions")) writes.push(request.url());
  });
  const title = `Early note ${Date.now()}`;
  await page.goto(`${hub.url}capture`);
  await page.getByLabel("Title", { exact: true }).fill(title);
  await page.getByLabel("Note", { exact: true }).fill("Typed before the project loaded.");
  const save = page.getByRole("button", { name: "Save note" });
  await expect(save).toBeDisabled();
  expect(writes).toEqual([]);
  release();
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByTestId("capture-saved")).toContainText(title);
  expect(writes).toHaveLength(1);
});

test("Dismiss this item is disabled until the project has loaded, so an early click is never silently dropped", async ({
  paired: page,
  hub,
  api,
}) => {
  // The same silent return as the typed note (Meeting.tsx dismiss() without a project). A meeting
  // of its own with one proposed item, so no other test's fixture is touched.
  const title = `Early dismissal ${Date.now()}`;
  const meeting = await api.post("/meetings", {
    title,
    text: "Agenda: units\nOpen: who checks the units?\n",
    request_id: crypto.randomUUID(),
  });
  const task = await api.post("/tasks/prepare", { purpose: "meeting_extract", meeting_id: meeting.meeting_id });
  await api.post(`/tasks/${task.task_id}/submit`, {
    request_id: crypto.randomUUID(),
    result: {
      purpose: "meeting_extract",
      items: [
        {
          type: "question",
          text: "Who checks the units?",
          evidence: [
            {
              source_id: meeting.source_id,
              version_id: meeting.version_id,
              locator: { type: "text", start_line: 2, end_line: 2 },
              quote_sha256: null,
            },
          ],
          proposed_owner: null,
          due_at: null,
        },
      ],
    },
  });

  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/project", async (route) => {
    await held;
    await route.continue();
  });
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/v1/transactions")) writes.push(request.url());
  });
  await page.goto(`${hub.url}meetings/${meeting.meeting_id}`);
  const card = page.getByTestId("meeting-item").filter({ hasText: "Who checks the units?" });
  await card.getByTestId("dismiss-start").click();
  const form = card.getByTestId("dismiss-form");
  await form.getByLabel(/Why is this item being dismissed/).fill("Answered in the meeting.");
  const dismiss = form.getByRole("button", { name: "Dismiss this item" });
  await expect(dismiss).toBeDisabled();
  expect(writes).toEqual([]);
  release();
  await expect(dismiss).toBeEnabled();
  await dismiss.click();
  await expect(card.getByTestId("dismissed-notice")).toBeVisible();
  expect(writes).toHaveLength(1);
});
