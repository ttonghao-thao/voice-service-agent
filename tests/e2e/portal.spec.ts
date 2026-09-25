import { test, expect } from "../../apps/web/node_modules/@playwright/test";
test("English text flow, citations, refreshed history and narrow layout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Support that responds" }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("Find the integration sample.");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".assistant-message")).toContainText(
    /synthetic integration excerpt/i,
  );
  await expect(page.locator(".citation-title")).toContainText("C1");
  await page.reload();
  await expect(page.getByText("Hello. How can I help today?")).toBeVisible();
  await expect(page.locator(".assistant-message")).toHaveCount(0);
  await page.screenshot({
    path: "../../artifacts/portal-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Call information" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../../artifacts/portal-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("Missing knowledge cannot appear as a real business answer", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("What is the nonexistent refund policy?");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".assistant-message")).toContainText("Insufficient evidence");
});
test("Text remains available when the microphone fails", async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException("Microphone permission denied", "NotAllowedError");
    };
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Your question" }),
  ).toBeEnabled();
});
