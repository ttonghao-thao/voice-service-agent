import { test, expect } from "../../apps/web/node_modules/@playwright/test";
test.use({
  launchOptions: {
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  },
  permissions: ["microphone"],
});
test("Synthetic microphone transport, playback stop, rotation and cleanup", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const frames: { epoch: number; seq: number; payload: { audio: string } }[] =
    [];
  page.on("websocket", (ws) =>
    ws.on("framesent", ({ payload }) => {
      try {
        const event = JSON.parse(String(payload));
        if (event.type === "portal.audio.append") frames.push(event);
      } catch {}
    }),
  );
  const main = page.getByRole("main");
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(main.getByText("Voice ready", { exact: true })).toBeVisible();
  await expect.poll(() => frames.length).toBeGreaterThan(10);
  const firstEpoch = frames[0].epoch;
  expect(Buffer.from(frames[0].payload.audio, "base64").length).toBe(3840);
  await page.getByRole("button", { name: "Mute microphone", exact: true }).click();
  await expect
    .poll(() => {
      const tail = frames.slice(-2);
      return (
        tail.length === 2 &&
        tail.every((f) =>
          Buffer.from(f.payload.audio, "base64").every((x) => x === 0),
        )
      );
    })
    .toBe(true);
  const beforeStop = frames.length;
  await page.getByRole("button", { name: "Stop playback", exact: true }).click();
  await expect.poll(() => frames.length).toBeGreaterThan(beforeStop);
  expect(frames.at(-1)?.epoch).toBe(firstEpoch);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(main.getByText("Voice disconnected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(main.getByText("Voice ready", { exact: true })).toBeVisible();
  await expect.poll(() => frames.filter((f) => f.seq === 0).length).toBe(2);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(main.getByText("Voice disconnected", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
