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
test("合成麦克风连续传输、静音、硬打断重建与结束释放", async ({ page }) => {
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
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话" }).click();
  await page.getByRole("button", { name: "开始语音", exact: true }).click();
  await expect(page.getByText("语音已就绪", { exact: true })).toBeVisible();
  await expect.poll(() => frames.length).toBeGreaterThan(10);
  const firstEpoch = frames[0].epoch;
  expect(Buffer.from(frames[0].payload.audio, "base64").length).toBe(3840);
  await page.getByRole("button", { name: "麦克风静音", exact: true }).click();
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
  await page.getByRole("button", { name: /打断并重新提问/ }).click();
  await expect(page.getByText("语音已就绪", { exact: true })).toBeVisible();
  await expect.poll(() => frames.some((f) => f.epoch > firstEpoch)).toBe(true);
  await page.getByRole("button", { name: "结束语音", exact: true }).click();
  await expect(page.getByText("语音未连接", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
