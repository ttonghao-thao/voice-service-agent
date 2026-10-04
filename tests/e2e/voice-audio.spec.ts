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

test("General voice answers stay in the chat without a knowledge turn", async ({ page }) => {
  let conversationId = "";
  let emit: (type: string, payload: Record<string, unknown>) => void;
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/conversations") && response.request().method() === "POST") {
      conversationId = (await response.json()).id;
    }
  });
  await page.routeWebSocket("**/api/v1/voice-sessions/**/stream?*", (socket) => {
    emit = (type, payload) => socket.send(JSON.stringify({
      type, event_id: crypto.randomUUID(), conversation_id: conversationId,
      epoch: 1, request_revision: 0, server_seq: 1, turn_id: null, payload,
    }));
    emit("portal.session.ready", { sample_rate: 24000, format: "pcm16", chunk_ms: 80, is_mock: true });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.getByRole("main").getByText("Voice ready", { exact: true })).toBeVisible();
  emit!("portal.transcript.done", { item_id: "general-input", text: "What is MLO?" });
  emit!("portal.speech_text.delta", {
    input_item_id: "general-input", response_id: "general-response", segment_index: 0,
    phase: "answer", answer_kind: "general", text: "MLO means multi-link ",
  });
  emit!("portal.speech_text.done", {
    input_item_id: "general-input", response_id: "general-response", segment_index: 0,
    phase: "answer", answer_kind: "general", text: "MLO means multi-link operation.",
  });
  emit!("portal.audio.done", { response_id: "general-response", phase: "answer" });
  await expect(page.locator(".user-message")).toContainText("What is MLO?");
  await expect(page.locator(".assistant-message")).toHaveCount(1);
  await expect(page.locator(".assistant-message")).toContainText("MLO means multi-link operation.");
  await expect(page.locator(".assistant-message")).toContainText("Model general answer");
  await expect(page.getByText("Waiting for request", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(page.locator(".assistant-message")).toContainText("MLO means multi-link operation.");
});
test("Synthetic microphone transport, playback stop, rotation and cleanup", async ({
  page,
}) => {
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
  await expect(main.getByText(/Microphone:/)).toBeVisible();
  await expect.poll(() => frames.length).toBeGreaterThan(10);
  const firstEpoch = frames[0].epoch;
  expect(Buffer.from(frames[0].payload.audio, "base64").length).toBe(3840);
  await page
    .getByRole("button", { name: "Mute microphone", exact: true })
    .click();
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
  await page
    .getByRole("button", { name: "Stop playback", exact: true })
    .click();
  await expect.poll(() => frames.length).toBeGreaterThan(beforeStop);
  expect(frames.at(-1)?.epoch).toBe(firstEpoch);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(
    main.getByText("Voice disconnected", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(main.getByText("Voice ready", { exact: true })).toBeVisible();
  await expect.poll(() => frames.filter((f) => f.seq === 0).length).toBe(2);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(
    main.getByText("Voice disconnected", { exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("Real AudioWorklet resumes a new reply after stop and rejects late old PCM", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const nativeConnect = AudioNode.prototype.connect;
    AudioWorkletNode.prototype.connect = function (destination: AudioNode) {
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 256;
      nativeConnect.call(this, analyser);
      nativeConnect.call(analyser, destination);
      (
        window as typeof window & { __playbackRms?: () => number }
      ).__playbackRms = () => {
        const values = new Float32Array(analyser.fftSize);
        analyser.getFloatTimeDomainData(values);
        return Math.sqrt(
          values.reduce((sum, value) => sum + value * value, 0) / values.length,
        );
      };
      return destination;
    } as typeof AudioWorkletNode.prototype.connect;
  });
  let sendEvent: (type: string, payload: Record<string, unknown>) => void;
  let stoppedResponse: unknown;
  await page.routeWebSocket(
    "**/api/v1/voice-sessions/**/stream?*",
    (socket) => {
      sendEvent = (type, payload) =>
        socket.send(JSON.stringify({ type, epoch: 1, payload }));
      socket.onMessage((message) => {
        const event = JSON.parse(String(message));
        if (event.type === "portal.playback.stop") {
          stoppedResponse = event.payload.response_id;
          sendEvent("portal.playback.clear", event.payload);
        }
      });
      sendEvent("portal.session.ready", {
        sample_rate: 24000,
        format: "pcm16",
        chunk_ms: 80,
        is_mock: true,
      });
    },
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(
    page.getByRole("main").getByText("Voice ready", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/24000 Hz sent/)).toBeVisible();
  const readRms = () =>
    page.evaluate(
      () =>
        (
          window as typeof window & { __playbackRms?: () => number }
        ).__playbackRms?.() ?? 0,
    );
  const tone = Buffer.alloc(3840);
  for (let i = 0; i < 1920; i++)
    tone.writeInt16LE(
      Math.round(12000 * Math.sin((2 * Math.PI * 1000 * i) / 24000)),
      i * 2,
    );
  const play = (response: string) => {
    for (let i = 0; i < 10; i++)
      sendEvent("portal.audio.delta", {
        response_id: response,
        audio: tone.toString("base64"),
      });
  };
  play("first");
  sendEvent!("portal.audio.done", { response_id: "first" });
  await expect.poll(readRms).toBeGreaterThan(0.05);
  await page
    .getByRole("button", { name: "Stop playback", exact: true })
    .click();
  await expect.poll(() => stoppedResponse).toBe("first");
  await expect.poll(readRms).toBeLessThan(0.001);
  play("first");
  await expect.poll(readRms).toBeLessThan(0.001);
  play("next");
  sendEvent!("portal.audio.done", { response_id: "next" });
  await expect.poll(readRms).toBeGreaterThan(0.05);
  sendEvent!("portal.playback.clear", { response_id: "first" });
  await expect.poll(readRms).toBeGreaterThan(0.05);
  sendEvent!("portal.playback.clear", { response_id: "next" });
  await expect.poll(readRms).toBeLessThan(0.001);
  await page.getByRole("button", { name: "End call", exact: true }).click();
});
