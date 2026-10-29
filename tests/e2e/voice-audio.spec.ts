import { test, expect } from "../../apps/web/node_modules/@playwright/test";
test.use({
  launchOptions: {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  },
  permissions: ["microphone"],
});

test("Knowledge reply groups interleaved follow-up and shows preparing after retrieval", async ({ page }) => {
  let conversationId = "";
  let emit: (type: string, payload: Record<string, unknown>) => void;
  let records: unknown[] = [];
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/conversations") && response.request().method() === "POST")
      conversationId = (await response.json()).id;
  });
  await page.route("**/api/v1/conversations/*/messages*", (route) => route.fulfill({ json: {
    items: [{ id: "pending", input_item_id: "first", user_text: "What about Product AX?", channel: "voice",
      epoch: 1, request_revision: 1, status: "running", execution_phase: "awaiting_provider_answer",
      answer: null, created_at: "2026-10-29T00:00:00Z" }],
    records, epoch: 1, request_revision: 1, next_before: null,
  } }));
  await page.routeWebSocket("**/api/v1/voice-sessions/**/stream?*", (socket) => {
    emit = (type, payload) => socket.send(JSON.stringify({ type, payload, event_id: crypto.randomUUID(),
      conversation_id: conversationId, epoch: 1, request_revision: 1, server_seq: 1, turn_id: null }));
    emit("portal.session.ready", { sample_rate: 24000, format: "pcm16", chunk_ms: 80, is_mock: true });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.getByRole("main").getByText("Voice ready", { exact: true })).toBeVisible();
  emit!("portal.tool.started", { message: "Sources received. Preparing the spoken reply." });
  await expect(page.getByText("Preparing reply", { exact: true })).toBeVisible();
  await expect(page.getByText("Sources received. Preparing the spoken reply.", { exact: true })).toBeVisible();
  emit!("portal.transcript.done", { item_id: "followup", text: "What is its connection limit?" });
  emit!("portal.speech_text.done", { response_id: "answer", input_item_id: "first",
    related_input_item_ids: ["followup"], text: "Product AX supports 10 connections.", answer_kind: "knowledge", phase: "answer" });
  await expect(page.getByText("Included in the knowledge reply above.", { exact: true })).toBeVisible();
  await expect(page.getByText("Model general answer", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Waiting for request", { exact: true })).toHaveCount(0);
  records = [{ kind: "user_transcript", epoch: 1, source_id: "followup", payload: { text: "What is its connection limit?" } },
    { kind: "voicechat_transcript", epoch: 1, source_id: "answer", payload: { text: "Product AX supports 10 connections.",
      input_item_id: "first", related_input_item_ids: ["followup"], answer_kind: "knowledge" } }];
  // Re-fetching history must retain grouping and the knowledge classification.
  emit!("portal.tool.started", { message: "Sources received. Preparing the spoken reply." });
  await expect(page.getByText("Included in the knowledge reply above.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "End call", exact: true }).click();
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

test("Microphone reconnects with a fresh voice session when the tool catalog changes", async ({ page }) => {
  let conversationId = "";
  const sessions: string[] = [];
  const sessionEpochs = new Map<string, number>();
  const connections: { emit: (type: string, payload: Record<string, unknown>) => void }[] = [];
  page.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/conversations") && response.request().method() === "POST") {
      conversationId = (await response.json()).id;
    }
  });
  await page.route("**/api/v1/conversations/*/voice-sessions", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    sessions.push(body.voice_session_id);
    sessionEpochs.set(body.voice_session_id, body.epoch);
    await route.fulfill({ response });
  });
  await page.routeWebSocket("**/api/v1/voice-sessions/**/stream?*", (socket) => {
    const sessionId = new URL(socket.url()).pathname.split("/").at(-2)!;
    const epoch = sessionEpochs.get(sessionId)!;
    const connection = { emit: (type: string, payload: Record<string, unknown>) => socket.send(JSON.stringify({
      type, event_id: crypto.randomUUID(), conversation_id: conversationId,
      epoch, request_revision: 0, server_seq: 1, turn_id: null, payload,
    })) };
    connections.push(connection);
    connection.emit("portal.session.ready", { sample_rate: 24000, format: "pcm16", chunk_ms: 80 });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.getByRole("main").getByText("Voice ready", { exact: true })).toBeVisible();
  await expect.poll(() => connections.length).toBe(1);
  connections[0].emit("portal.error", { code: "VOICE_TOOL_CONFIGURATION_CHANGED", message: "Voice tools changed" });
  await expect.poll(() => connections.length).toBe(2);
  await expect(page.getByRole("main").getByText("Voice ready", { exact: true })).toBeVisible();
  await expect.poll(() => sessions.length).toBe(2);
  expect(new Set(sessions).size).toBe(2);
  await page.getByRole("button", { name: "End call", exact: true }).click();
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
