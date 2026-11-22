import { test, expect } from "../../apps/web/node_modules/@playwright/test";

test("Profile preparation cannot race voice startup or resurrect an ended call", async ({ page, request }) => {
  const caps = await (await request.get("/api/v1/capabilities")).json();
  test.skip(caps.provider !== "sophon_live", "Requires the Sophon application API");
  let release = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let cid = "", issued = 0;
  await page.route("**/api/v1/conversations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    issued++;
    await pending;
    const response = await route.fetch(); cid = (await response.json()).id;
    await route.fulfill({ response });
  });
  const removed: string[] = [];
  page.on("request", (r) => { if (r.method() === "DELETE") removed.push(new URL(r.url()).pathname); });
  await page.goto("/");
  await page.getByRole("button", { name: "Voice settings", exact: true }).click();
  await expect.poll(() => issued).toBe(1);
  await expect(page.getByRole("button", { name: "Start call", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "End call", exact: true }).click();
  release();
  await expect.poll(() => cid).not.toBe("");
  await expect.poll(() => removed).toContain(`/api/v1/conversations/${cid}`);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(issued).toBe(1);
});

test("Sophon profiles persist styles and preview the fixed tool policy", async ({ page, request }) => {
  const caps = await (await request.get("/api/v1/capabilities")).json();
  test.skip(caps.provider !== "sophon_live", "Requires the Sophon application API; no model responses are mocked");
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "Voice settings", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByLabel("Final Instructions preview")).toContainText("delegate");
  await page.getByRole("button", { name: "New profile", exact: true }).click();
  await page.getByLabel("Profile name", { exact: true }).fill("小狗");
  await page.getByLabel("Profile description").fill("一个友善的语音伙伴");
  await page.getByLabel("Language style").fill("说话活泼");
  await expect(page.getByLabel("Final Instructions preview")).toHaveValue(/你是小狗.*说话活泼/);
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(page.getByRole("button", { name: "Delete profile", exact: true })).toBeEnabled();
  await page.getByLabel("Language style").fill("说话沉稳");
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save profile", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Voice settings", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByLabel("Language style")).toHaveValue("说话沉稳");
  await expect(page.getByLabel("Final Instructions preview")).toHaveValue(/delegate/);
  await expect(page.getByRole("dialog")).toBeInViewport({ ratio: 1 });
  if (process.env.SOPHON_PROFILE_SCREENSHOT) await page.getByRole("dialog").screenshot({ path: process.env.SOPHON_PROFILE_SCREENSHOT });
  await page.getByRole("button", { name: "Delete profile", exact: true }).click();
  await expect(page.getByLabel("Profile name", { exact: true })).toHaveValue("闹闹");
  expect(errors).toEqual([]);
});
