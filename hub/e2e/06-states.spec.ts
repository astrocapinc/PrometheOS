// Loading, empty, error (not found), offline and unpaired states on the real hub.
import { expect, nav, pairContext, shot, test } from "./fixtures";

test("offline: an unreachable hub shows the offline state and banner; recovery reloads the view", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}notebook`);
  await expect(page.getByTestId("entry-card").first()).toBeVisible();
  await page.route("**/api/v1/**", (route) =>
    route.abort("internetdisconnected"),
  );
  await nav(page, "Knowledge").click();
  const error = page.locator('[data-error-kind="offline"]').first();
  await expect(error).toContainText("Offline");
  await expect(page.getByTestId("offline-banner")).toContainText(
    "Offline: the hub cannot be reached",
  );
  await shot(page, "state-offline");

  await page.unroute("**/api/v1/**");
  await error.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("claim-card").first()).toBeVisible();
  await expect(page.getByTestId("offline-banner")).toHaveCount(0);
});

test("offline after the server stops answering every route (each destination shows offline)", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  await page.route("**/api/v1/**", (route) => route.abort("connectionrefused"));
  for (const name of ["Notebook", "Knowledge", "Papers"] as const) {
    await nav(page, name).click();
    await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
    await expect(
      page.locator('[data-error-kind="offline"]').first(),
    ).toBeVisible();
  }
});

test("loading and empty states", async ({ paired: page, hub }) => {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/notebook", async (route) => {
    await gate;
    await route.continue();
  });
  await page.goto(`${hub.url}notebook`);
  await expect(
    page.getByRole("status").filter({ hasText: "Loading notebook" }),
  ).toBeVisible();
  release();
  await expect(page.getByTestId("entry-card").first()).toBeVisible();

  await nav(page, "Knowledge").click();
  await page
    .getByLabel("Search findings, evidence, questions and claims")
    .fill("zzqx-no-such-term");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("search-results")).toContainText(
    "No matches for “zzqx-no-such-term”",
  );
  // An empty papers list (served as empty for this check) shows its empty state.
  await page.route("**/api/v1/records?kind=paper*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], next_cursor: null, project_seq: 0 }),
    }),
  );
  await nav(page, "Papers").click();
  await expect(page.getByText("No papers yet.")).toBeVisible();
  await expect(page.getByTestId("papers-scope")).toContainText("not a score");
  await page.unroute("**/api/v1/records?kind=paper*");
});

test("not found: an unknown meeting shows a not-found error, not a blank page", async ({
  paired: page,
  hub,
}) => {
  await page.goto(
    `${hub.url}meetings/meeting_00000000-0000-4000-8000-000000000000`,
  );
  await expect(page.getByRole("alert").first()).toContainText("Not found");
  await page.goto(`${hub.url}no/such/page`);
  await expect(
    page.getByRole("heading", { level: 1, name: "Page not found" }),
  ).toBeVisible();
});

test("unpaired: a deep link asks to pair first, then opens the requested destination", async ({
  page,
  hub,
}) => {
  await page.goto(`${hub.url}papers`);
  await expect(
    page.getByRole("heading", { name: "Pair this browser" }),
  ).toBeVisible();
  await pairContext(page, hub);
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: "Papers" }),
  ).toBeVisible();
});

test("session ends mid-use (401): the next request returns the browser to pairing", async ({
  paired: page,
  hub,
}) => {
  await page.goto(`${hub.url}now`);
  await expect(page.getByTestId("objective")).toBeVisible();
  await page.context().clearCookies();
  const pairing = page.getByRole("heading", { name: "Pair this browser" });
  // The background changes long-poll may receive the 401 first and replace the navigation
  // with pairing before this click; either request must end in pairing (decision 0009 item 8).
  if (!(await pairing.isVisible())) {
    await nav(page, "Notebook")
      .click({ timeout: 5000 })
      .catch(() => undefined);
  }
  await expect(pairing).toBeVisible();
});

test("permission denied: a 403 from the hub shows a 'Not permitted' state, not a blank page", async ({
  paired: page,
  hub,
}) => {
  await page.route("**/api/v1/notebook", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        ok: false,
        error: {
          code: "policy_denied",
          message: "owner scope needed for this view",
          retryable: false,
          details: {},
        },
        project_seq: null,
      }),
    }),
  );
  await page.goto(`${hub.url}notebook`);
  const alert = page.getByRole("alert").first();
  await expect(alert).toContainText("Not permitted");
  await expect(alert).toContainText("owner scope needed for this view");
  await page.unroute("**/api/v1/notebook");
});
