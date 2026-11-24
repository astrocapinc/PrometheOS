import { expect, nextToken, shot, test } from "./fixtures";

test("unpaired browser gets the pairing form; wrong code refused; right code pairs, survives reload, signs out", async ({
  page,
  hub,
}) => {
  await page.goto(`${hub.url}knowledge`);
  await expect(page.getByRole("heading", { name: "Pair this browser" })).toBeVisible();
  const form = page.locator("form");
  await expect(form).toHaveAttribute("method", "post");
  await shot(page, "pairing");

  await page.getByLabel("Pairing code").fill("not-a-real-code");
  await page.getByRole("button", { name: "Pair" }).click();
  await expect(page.getByRole("alert")).toContainText("wrong, already used or expired");

  const token = nextToken(hub);
  await page.getByLabel("Pairing code").fill(token);
  await page.getByRole("button", { name: "Pair" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
  // The code never reaches the URL, history or web storage; the session cookie is HttpOnly.
  expect(page.url()).toBe(`${hub.url}knowledge`);
  const stored = await page.evaluate(() =>
    JSON.stringify({ local: { ...window.localStorage }, session: { ...window.sessionStorage } }),
  );
  expect(stored).not.toContain(token);
  expect(stored.toLowerCase()).not.toContain("csrf");
  const cookie = (await page.context().cookies()).find((c) => c.name === "prometheos_hub");
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe("Strict");

  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Pair this browser" })).toBeVisible();
  // The used code is refused the second time (one use).
  await page.getByLabel("Pairing code").fill(token);
  await page.getByRole("button", { name: "Pair" }).click();
  await expect(page.getByRole("alert")).toContainText("wrong, already used or expired");
  const session = await page.request.get(`${hub.url}api/v1/session`);
  expect((await session.json()).paired).toBe(false);
});
