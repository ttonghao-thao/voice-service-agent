import { expect, test } from "../../apps/web/node_modules/@playwright/test";

const sample = process.env.LIVEKIT_E2E_WAV;
test.skip(!sample, "Requires the local LiveKit and Sophon Gateway fixture from docs/validation.md");

test("Two rooms deliver a delegated request without uploading PCM over the control socket", async ({ browser }) => {
  await Promise.all(Array.from({ length: 2 }, async () => {
    const context = await browser.newContext();
    const page = await context.newPage();
    let audioFrames = 0;
    page.on("websocket", (ws) => {
      if (!ws.url().includes("/api/")) return;
      ws.on("framesent", ({ payload }) => {
        if (JSON.parse(String(payload)).type === "portal.audio.append") audioFrames++;
      });
    });

    try {
      await page.goto("/");
      await page.getByText("Audio file", { exact: true }).click();
      await page.getByLabel("Recorded WAV file").setInputFiles(sample!);
      await page.getByRole("button", { name: "Start file call" }).click();
      await expect(page.getByRole("main").getByText("Voice ready", { exact: true })).toBeVisible();
      await expect(page.getByText("Delegated request", { exact: true })).toBeVisible();
      await expect(page.locator(".assistant-message")).toContainText("Spoken reply unverified");
      const audio = page.locator("audio");
      await expect(audio).toHaveCount(1);
      expect(await audio.evaluate((element: HTMLAudioElement) => element.srcObject instanceof MediaStream)).toBe(true);
      expect(audioFrames).toBe(0);
      await page.getByRole("button", { name: "Stop playback", exact: true }).click();
      await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.muted)).toBe(true);
      await page.getByRole("button", { name: "End call", exact: true }).click();
      await expect(audio).toHaveCount(0);
    } finally {
      await context.close();
    }
  }));
});
