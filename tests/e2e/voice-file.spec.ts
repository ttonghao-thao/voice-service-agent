import {
  test,
  expect,
  type Page,
} from "../../apps/web/node_modules/@playwright/test";

function recordingWave(
  sampleCount = 2400,
  options: { format?: { channels: number } } = {},
) {
  const bytes = Buffer.alloc(44 + sampleCount * 2);
  bytes.write("RIFF", 0);
  bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write("WAVEfmt ", 8);
  bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20);
  bytes.writeUInt16LE(options.format?.channels ?? 1, 22);
  bytes.writeUInt32LE(24000, 24);
  bytes.writeUInt32LE(48000, 28);
  bytes.writeUInt16LE(2, 32);
  bytes.writeUInt16LE(16, 34);
  bytes.write("data", 36);
  bytes.writeUInt32LE(sampleCount * 2, 40);
  for (let i = 0; i < sampleCount; i++)
    bytes.writeInt16LE(
      Math.round(12000 * Math.sin((2 * Math.PI * 1000 * i) / 24000)),
      44 + i * 2,
    );
  return bytes;
}

interface FileFrame {
  seq: number;
  epoch: number;
  payload: { format: string; sample_rate: number; audio: string };
  at: number;
}

interface FileConnection {
  frames: FileFrame[];
  emit(type: string, payload?: Record<string, unknown>): void;
}

async function filePortal(
  page: Page,
  options: {
    beforeConversation?: () => Promise<void>;
    beforeText?: () => Promise<void>;
  } = {},
) {
  const calls: string[] = [];
  const conversationHeaders: (string | undefined)[] = [];
  const authorizedSessions: string[] = [];
  const deletions: { path: string; authorization?: string }[] = [];
  const textRequests: { path: string; text: string }[] = [];
  let textRevision = 0;
  const connections: FileConnection[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const counters = { microphone: 0, sourceStarts: 0 };
    const target = window as typeof window & {
      __fileTest?: typeof counters;
      __filePlaybackRms?: () => number;
    };
    target.__fileTest = counters;
    navigator.mediaDevices.getUserMedia = async () => {
      counters.microphone++;
      throw new DOMException(
        "File tests must not request a microphone",
        "NotAllowedError",
      );
    };
    const start = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function (...args) {
      counters.sourceStarts++;
      start.apply(this, args);
    };
    const connect = AudioNode.prototype.connect;
    AudioWorkletNode.prototype.connect = function (destination: AudioNode) {
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 256;
      connect.call(this, analyser);
      connect.call(analyser, destination);
      target.__filePlaybackRms = () => {
        const samples = new Float32Array(analyser.fftSize);
        analyser.getFloatTimeDomainData(samples);
        return Math.sqrt(
          samples.reduce((sum, sample) => sum + sample * sample, 0) /
            samples.length,
        );
      };
      return destination;
    } as typeof AudioWorkletNode.prototype.connect;
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/capabilities")) {
      await route.fulfill({
        json: {
          is_mock: true,
          voice_available: true,
          text_configured: true,
          agent_provider: "compatible",
          cuekb_mode: "mock",
          weather_mode: "mock",
          provider: "nvidia",
          voice_session_max_seconds: 105,
        },
      });
    } else if (
      path === "/api/v1/conversations" &&
      request.method() === "POST"
    ) {
      const id = `file-call-${calls.length + 1}`;
      calls.push(id);
      conversationHeaders.push(request.headers().authorization);
      await options.beforeConversation?.();
      await route.fulfill({
        status: 201,
        json: {
          id,
          title: "File test",
          epoch: 0,
          request_revision: 0,
          access_token: `token-${id}`,
        },
      });
    } else if (
      path.endsWith("/voice-sessions") &&
      request.method() === "POST"
    ) {
      const id = path.split("/").at(-2)!;
      authorizedSessions.push(request.headers().authorization);
      await route.fulfill({
        status: 201,
        json: {
          voice_session_id: `sid-${id}`,
          epoch: 1,
          request_revision: 0,
          ws_url: `/api/v1/voice-sessions/sid-${id}/stream?ticket=fixture`,
        },
      });
    } else if (path.endsWith("/events")) {
      await route.fulfill({ contentType: "text/event-stream", body: "" });
    } else if (path.endsWith("/messages") && request.method() === "POST") {
      textRequests.push({ path, text: request.postDataJSON().text });
      await options.beforeText?.();
      textRevision = 1;
      await route.fulfill({
        status: 202,
        json: {
          turn_id: "controlled-text-turn",
          epoch: 0,
          request_revision: 1,
          status: "running",
        },
      });
    } else if (path.endsWith("/messages")) {
      await route.fulfill({
        json: {
          epoch: 0,
          request_revision: textRevision,
          items: [],
          records: [],
          next_before: null,
        },
      });
    } else if (request.method() === "DELETE") {
      deletions.push({ path, authorization: request.headers().authorization });
      await route.fulfill({ json: {} });
    } else
      throw new Error(
        `Unexpected file-test API request: ${request.method()} ${path}`,
      );
  });
  await page.routeWebSocket(
    "**/api/v1/voice-sessions/**/stream?*",
    (socket) => {
      let serverSeq = 0;
      const connection: FileConnection = {
        frames: [],
        emit(type, payload = {}) {
          socket.send(
            JSON.stringify({
              type,
              epoch: 1,
              payload,
              server_seq: ++serverSeq,
              event_id: `file-event-${serverSeq}`,
              request_revision: 0,
              conversation_id: calls.at(-1),
              turn_id: null,
            }),
          );
        },
      };
      socket.onMessage((raw) => {
        const event = JSON.parse(String(raw));
        if (event.type === "portal.audio.append")
          connection.frames.push({ ...event, at: Date.now() });
      });
      connections.push(connection);
    },
  );
  await page.goto("/");
  // Ant Design button radios hide the native input; operate the visible label.
  await page.getByText("Audio file", { exact: true }).click();
  return {
    calls,
    conversationHeaders,
    authorizedSessions,
    deletions,
    textRequests,
    connections,
    errors,
  };
}

async function selectWave(page: Page, sampleCount = 48000, name = "fixed.wav") {
  await page.getByLabel("Recorded WAV file").setInputFiles({
    name,
    mimeType: "audio/wav",
    buffer: Buffer.from(recordingWave(sampleCount)),
  });
  await expect(
    page.getByRole("button", { name: "Start file call", exact: true }),
  ).toBeEnabled();
}

async function startFile(
  page: Page,
  fixture: Awaited<ReturnType<typeof filePortal>>,
  callIndex = 0,
) {
  await page
    .getByRole("button", { name: "Start file call", exact: true })
    .click();
  await expect.poll(() => fixture.connections.length).toBe(callIndex + 1);
  fixture.connections[callIndex].emit("portal.session.ready", {
    format: "pcm16",
    sample_rate: 24000,
    chunk_ms: 80,
  });
  await expect(
    page.getByRole("main").getByText("Voice ready", { exact: true }),
  ).toBeVisible();
  return fixture.connections[callIndex];
}

test("Invalid or excessive recorded WAV files cannot create a call or upload audio", async ({
  page,
}) => {
  const fixture = await filePortal(page);
  const cases = [
    { name: "invalid.wav", bytes: new ArrayBuffer(44), message: /RIFF WAV/ },
    {
      name: "stereo.wav",
      bytes: recordingWave(2400, { format: { channels: 2 } }),
      message: /24 kHz, mono, PCM16/,
    },
    { name: "long.wav", bytes: recordingWave(720001), message: /30 seconds/ },
    {
      name: "large.wav",
      bytes: new ArrayBuffer(2 * 1024 * 1024 + 1),
      message: /2 MiB/,
    },
  ];
  for (const sample of cases) {
    await page.getByLabel("Recorded WAV file").setInputFiles({
      name: sample.name,
      mimeType: "audio/wav",
      buffer: Buffer.from(sample.bytes),
    });
    await expect(page.getByRole("alert")).toContainText(sample.message);
    await expect(
      page.getByRole("button", { name: "Start file call", exact: true }),
    ).toBeDisabled();
  }
  expect(fixture.calls).toEqual([]);
  expect(fixture.connections).toEqual([]);
  expect(fixture.errors).toEqual([]);
});

test("Real file source waits for ready, streams in real time without microphone or echo and flushes EOF", async ({
  page,
}) => {
  const fixture = await filePortal(page);
  await selectWave(page);
  await expect(
    page.getByText("2.00 s · 24000 Hz · mono PCM16", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Start file call", exact: true })
    .click();
  await expect.poll(() => fixture.connections.length).toBe(1);
  await expect(
    page
      .getByRole("status", { name: "" })
      .filter({ hasText: "Waiting for voice connection" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => (window as typeof window & { __fileTest?: unknown }).__fileTest,
    ),
  ).toEqual({ microphone: 0, sourceStarts: 0 });
  expect(fixture.connections[0].frames).toEqual([]);
  fixture.connections[0].emit("portal.session.ready", {
    format: "pcm16",
    sample_rate: 24000,
    chunk_ms: 80,
  });
  await expect
    .poll(() => fixture.connections[0].frames.length)
    .toBeGreaterThan(3);
  await expect(
    page.getByRole("radio", { name: "Microphone", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Mute microphone", exact: true }),
  ).toHaveCount(0);
  const beforeStop = fixture.connections[0].frames.length;
  await page
    .getByRole("button", { name: "Stop playback", exact: true })
    .click();
  await expect
    .poll(() => fixture.connections[0].frames.length)
    .toBeGreaterThan(beforeStop + 2);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as typeof window & { __filePlaybackRms?: () => number }
          ).__filePlaybackRms?.() ?? 1,
      ),
    )
    .toBeLessThan(0.001);
  await expect(page.locator(".recording-progress [role=status]")).toHaveText(
    "Recording sent. Waiting for reply",
  );
  await expect(
    page.getByRole("progressbar", { name: "Recording send progress" }),
  ).toHaveAttribute("value", "48000");
  const connection = fixture.connections[0];
  await expect
    .poll(() =>
      connection.frames
        .slice(-2)
        .every((frame) =>
          Buffer.from(frame.payload.audio, "base64").every(
            (value) => value === 0,
          ),
        ),
    )
    .toBe(true);
  const voiced = connection.frames.filter((frame) =>
    Buffer.from(frame.payload.audio, "base64").some((value) => value !== 0),
  );
  expect(voiced.length).toBeGreaterThanOrEqual(25);
  expect(voiced.at(-1)!.at - voiced[0].at).toBeGreaterThan(1400);
  for (let i = 0; i < connection.frames.length; i++) {
    expect(connection.frames[i].seq).toBe(i);
    expect(connection.frames[i].payload.format).toBe("pcm16");
    expect(connection.frames[i].payload.sample_rate).toBe(24000);
    expect(
      Buffer.from(connection.frames[i].payload.audio, "base64"),
    ).toHaveLength(3840);
    expect(connection.frames[i].payload).not.toHaveProperty("fileSamples");
  }
  connection.emit("portal.transcript.done", {
    item_id: "fixed-source",
    text: "Controlled recorded input",
  });
  await expect(page.locator(".user-message")).toContainText(
    "Controlled recorded input",
  );
  await page.getByRole("button", { name: "End call", exact: true }).click();
  await expect(page.locator(".recording-progress [role=status]")).toHaveText(
    "File call ended",
  );
  expect(
    await page.evaluate(
      () => (window as typeof window & { __fileTest?: unknown }).__fileTest,
    ),
  ).toEqual({ microphone: 0, sourceStarts: 1 });
  expect(fixture.errors).toEqual([]);
});

test("Rejection and session rotation end an incomplete file; an explicit rerun creates independent call access", async ({
  page,
}) => {
  const fixture = await filePortal(page);
  await selectWave(page, 96000);
  const first = await startFile(page, fixture);
  await expect.poll(() => first.frames.length).toBeGreaterThan(2);
  first.emit("portal.transcript.done", {
    item_id: "old-input",
    text: "Previous file input",
  });
  first.emit("portal.error", {
    code: "VOICE_TOOL_REQUIRED",
    message: "Rejected recorded input",
  });
  await expect(page.locator(".recording-progress [role=status]")).toHaveText(
    "Recording input interrupted",
  );
  await expect(page.locator(".recording-progress")).toContainText(/s not sent/);
  await expect(
    page.getByRole("main").getByText("Voice disconnected", { exact: true }),
  ).toBeVisible();
  expect(fixture.connections).toHaveLength(1);
  const second = await startFile(page, fixture, 1);
  await expect(page.locator(".user-message")).toHaveCount(0);
  await expect.poll(() => second.frames.length).toBeGreaterThan(2);
  second.emit("portal.error", {
    code: "VOICE_SESSION_ROTATION_REQUIRED",
    message: "Session limit",
  });
  await expect(page.locator(".recording-progress [role=status]")).toHaveText(
    "Recording input interrupted",
  );
  await expect(page.getByRole("alert")).toContainText("Start a new file call");
  await expect(
    page.getByRole("main").getByText("Voice disconnected", { exact: true }),
  ).toBeVisible();
  expect(fixture.calls).toEqual(["file-call-1", "file-call-2"]);
  expect(fixture.authorizedSessions).toEqual([
    "Bearer token-file-call-1",
    "Bearer token-file-call-2",
  ]);
  expect(fixture.connections).toHaveLength(2);
  expect(second.frames[0].seq).toBe(0);
  expect(
    await page.evaluate(
      () => (window as typeof window & { __fileTest?: unknown }).__fileTest,
    ),
  ).toEqual({ microphone: 0, sourceStarts: 2 });
  expect(fixture.errors).toEqual([]);
});

test("Ending a file call during capability issuance closes the late conversation without starting its audio", async ({
  page,
}) => {
  let release = () => {};
  const issued = new Promise<void>((resolve) => {
    release = resolve;
  });
  const fixture = await filePortal(page, { beforeConversation: () => issued });
  await selectWave(page);
  await page
    .getByRole("button", { name: "Start file call", exact: true })
    .click();
  await expect.poll(() => fixture.calls.length).toBe(1);
  await expect(
    page.getByRole("button", { name: "End call", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect(fixture.connections).toEqual([]);
  release();
  await expect
    .poll(() => fixture.deletions)
    .toContainEqual({
      path: "/api/v1/conversations/file-call-1",
      authorization: "Bearer token-file-call-1",
    });
  await expect(
    page.getByRole("button", { name: "Start file call", exact: true }),
  ).toBeEnabled();
  expect(fixture.authorizedSessions).toEqual([]);
  expect(fixture.connections).toEqual([]);
  expect(
    await page.evaluate(
      () => (window as typeof window & { __fileTest?: unknown }).__fileTest,
    ),
  ).toEqual({ microphone: 0, sourceStarts: 0 });
  // A later explicit call must have fresh access; the unused capability was cleared.
  const second = await startFile(page, fixture);
  await expect.poll(() => second.frames.length).toBeGreaterThan(0);
  expect(fixture.calls).toEqual(["file-call-1", "file-call-2"]);
  expect(fixture.conversationHeaders).toEqual([undefined, undefined]);
  expect(fixture.authorizedSessions).toEqual(["Bearer token-file-call-2"]);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect(fixture.errors).toEqual([]);
});

test("Tool configuration refresh stops file input without replaying it; explicit rerun creates a new call", async ({ page }) => {
  const fixture = await filePortal(page);
  await selectWave(page, 96000);
  const first = await startFile(page, fixture);
  await expect.poll(() => first.frames.length).toBeGreaterThan(2);
  first.emit("portal.error", {
    code: "VOICE_TOOL_CONFIGURATION_CHANGED",
    message: "Voice tools changed",
  });
  await expect(page.locator(".recording-progress [role=status]")).toHaveText("Recording input interrupted");
  await expect(page.getByRole("alert")).toContainText("the recording was not replayed");
  expect(fixture.connections).toHaveLength(1);
  expect(await page.evaluate(() => (window as typeof window & { __fileTest?: unknown }).__fileTest))
    .toEqual({ microphone: 0, sourceStarts: 1 });
  const next = await startFile(page, fixture, 1);
  await expect.poll(() => next.frames.length).toBeGreaterThan(2);
  expect(fixture.calls).toEqual(["file-call-1", "file-call-2"]);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect(fixture.errors).toEqual([]);
});

test("A pending text submission prevents starting a competing file call", async ({
  page,
}) => {
  let release = () => {};
  const accepted = new Promise<void>((resolve) => {
    release = resolve;
  });
  const fixture = await filePortal(page, { beforeText: () => accepted });
  await selectWave(page);
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("Controlled pending text request");
  await page
    .getByRole("button", { name: "Send question", exact: true })
    .click();
  await expect.poll(() => fixture.textRequests.length).toBe(1);
  await expect(
    page.getByRole("button", { name: "Start file call", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("radio", { name: "Microphone", exact: true }),
  ).toBeDisabled();
  expect(fixture.calls).toEqual(["file-call-1"]);
  expect(fixture.connections).toEqual([]);
  expect(fixture.authorizedSessions).toEqual([]);
  expect(
    await page.evaluate(
      () => (window as typeof window & { __fileTest?: unknown }).__fileTest,
    ),
  ).toEqual({ microphone: 0, sourceStarts: 0 });
  release();
  await expect(
    page.getByRole("button", { name: "Start file call", exact: true }),
  ).toBeEnabled();
  expect(fixture.textRequests).toEqual([
    {
      path: "/api/v1/conversations/file-call-1/messages",
      text: "Controlled pending text request",
    },
  ]);
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect(fixture.errors).toEqual([]);
});
