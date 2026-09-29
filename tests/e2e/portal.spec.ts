import { test, expect } from "../../apps/web/node_modules/@playwright/test";
test("English text flow, citations, refreshed history and narrow layout", async ({
  page,
}) => {
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
  await expect(
    page.getByRole("button", { name: "Call information" }),
  ).toBeVisible();
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
test("Missing knowledge cannot appear as a real business answer", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("What is the nonexistent refund policy?");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".assistant-message")).toContainText(
    "Insufficient evidence",
  );
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

test("Voice turns render as paired chat instead of transcript history", async ({
  page,
}) => {
  await page.route("**/api/v1/capabilities", async (route) => {
    await route.fulfill({
      json: {
        is_mock: false,
        voice_available: true,
        text_configured: true,
        agent_provider: "compatible",
        cuekb_mode: "real",
        weather_mode: "mock",
        provider: "nvidia",
        voice_session_max_seconds: 105,
      },
    });
  });
  await page.route("**/api/v1/conversations", async (route) => {
    await route.fulfill({
      status: 201,
      json: {
        id: "voice-conversation",
        title: "Voice question",
        epoch: 0,
        request_revision: 0,
        locale: "en-US",
        access_token: "test-call-token",
      },
    });
  });
  await page.route(
    "**/api/v1/conversations/voice-conversation/messages**",
    async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          json: {
            turn_id: "voice-turn",
            epoch: 0,
            request_revision: 1,
            status: "running",
          },
        });
        return;
      }
      await route.fulfill({
        json: {
          epoch: 0,
          request_revision: 1,
          next_before: null,
          items: [
            {
              id: "voice-turn",
              user_text: "Find the mooncake product guide",
              channel: "voice",
              status: "answered",
              answer: {
                answer_id: "answer-1",
                status: "answered",
                display_text: "Voice knowledge answer",
                speech_text: "Voice knowledge answer",
                citations: [],
                cards: [],
                is_mock: false,
                reason_code: null,
              },
              epoch: 0,
              request_revision: 1,
              parent_task_id: null,
              cancellation_reason: null,
              delivery_status: "accepted",
              output_suppressed: false,
            },
          ],
        },
      });
    },
  );
  await page.route(
    "**/api/v1/conversations/voice-conversation/events",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "",
      });
    },
  );

  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("Open voice history");
  await page.getByRole("button", { name: "Send question" }).click();

  const turn = page.locator(".turn");
  await expect(turn).toContainText("You · Voice request");
  await expect(turn).toContainText("Find the mooncake product guide");
  await expect(turn).toContainText("Voice knowledge answer");
  await expect(page.locator(".live-captions")).toHaveCount(0);
});

test("Voice transcript becomes one persistent chat message and retains spoken text", async ({
  page,
}) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const destination = context.createMediaStreamDestination();
      (
        window as typeof window & { __fakeMicrophoneContext?: AudioContext }
      ).__fakeMicrophoneContext = context;
      return destination.stream;
    };
    class FakeWebSocket {
      static OPEN = 1;
      readyState = 1;
      bufferedAmount = 0;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      send() {}
      close() {
        this.readyState = 3;
        this.onclose?.();
      }
      constructor() {
        (
          window as typeof window & { __fakeVoiceSocket?: FakeWebSocket }
        ).__fakeVoiceSocket = this;
        setTimeout(() => {
          this.emit("portal.session.ready", {
            sample_rate: 24000,
            format: "pcm16",
            chunk_ms: 80,
            is_mock: false,
          });
        }, 50);
      }
      emit(type: string, payload: Record<string, unknown>) {
        this.onmessage?.(
          new MessageEvent("message", {
            data: JSON.stringify({
              type,
              event_id: crypto.randomUUID(),
              conversation_id: "caption-conversation",
              epoch: 1,
              request_revision: 1,
              server_seq: 1,
              turn_id: payload.turn_id || null,
              payload,
            }),
          }),
        );
      }
    }
    (window as typeof window & { WebSocket: typeof WebSocket }).WebSocket =
      FakeWebSocket as unknown as typeof WebSocket;
  });
  await page.route("**/api/v1/capabilities", async (route) => {
    await route.fulfill({
      json: {
        is_mock: false,
        voice_available: true,
        text_configured: true,
        agent_provider: "compatible",
        cuekb_mode: "real",
        weather_mode: "mock",
        provider: "nvidia",
        voice_session_max_seconds: 105,
      },
    });
  });
  await page.route("**/api/v1/conversations", async (route) => {
    await route.fulfill({
      status: 201,
      json: {
        id: "caption-conversation",
        title: "Voice question",
        epoch: 0,
        request_revision: 0,
        locale: "en-US",
        access_token: "caption-token",
      },
    });
  });
  await page.route(
    "**/api/v1/conversations/caption-conversation/voice-sessions",
    async (route) => {
      await route.fulfill({
        status: 201,
        json: {
          voice_session_id: "caption-session",
          epoch: 1,
          request_revision: 0,
          ws_url: "/api/v1/voice-sessions/caption-session/stream?ticket=test",
        },
      });
    },
  );
  let messageReads = 0;
  let secondTurn = false;
  await page.route(
    "**/api/v1/conversations/caption-conversation/messages**",
    async (route) => {
      messageReads += 1;
      await route.fulfill({
        json: {
          epoch: 1,
          request_revision: 1,
          next_before: null,
          records: [],
          items:
            messageReads === 1
              ? []
              : [
                  {
                    id: "voice-turn",
                    input_item_id: "input-1",
                    created_at: "2026-09-29T00:00:00Z",
                    user_text: "Find the actual product guide",
                    channel: "voice",
                    status: "running",
                    answer: null,
                    epoch: 1,
                    request_revision: 1,
                    parent_task_id: null,
                    cancellation_reason: null,
                    delivery_status: "pending_validation",
                    output_suppressed: false,
                  },
                  ...(secondTurn
                    ? [
                        {
                          id: "voice-turn-2",
                          input_item_id: "input-2",
                          created_at: "2026-09-29T00:00:01Z",
                          user_text: "Find the actual product guide",
                          channel: "voice",
                          status: "running",
                          answer: null,
                          epoch: 1,
                          request_revision: 2,
                          parent_task_id: "voice-turn",
                          cancellation_reason: null,
                          delivery_status: "pending_validation",
                          output_suppressed: false,
                        },
                      ]
                    : []),
                ],
        },
      });
    },
  );
  await page.route(
    "**/api/v1/conversations/caption-conversation/events",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "",
      });
    },
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(
    page.getByRole("main").getByText("Voice ready", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.transcript.delta", {
      item_id: "input-1",
      text: "Find the actual ",
    });
    socket?.emit("portal.transcript.done", {
      item_id: "input-1",
      text: "Find the actual product guide",
    });
  });
  await expect(page.locator(".user-message")).toContainText(
    "Find the actual product guide",
  );
  await expect(page.locator(".turn")).toHaveCount(1);
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.tool.started", {
      message: "Processing",
      user_text: "Find the actual product guide",
    });
  });
  await expect(page.locator(".turn")).toContainText(
    "Find the actual product guide",
  );
  await expect(page.locator(".turn")).toHaveCount(1);
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.answer.final", {
      turn_id: "voice-turn",
      answer_id: "answer-1",
      status: "answered",
      display_text: "The verified full answer.",
      speech_text: "The spoken answer.",
      citations: [],
      cards: [],
      is_mock: false,
      reason_code: null,
    });
    socket?.emit("portal.audio.done", {
      response_id: "answer-1",
      turn_id: "voice-turn",
      phase: "answer",
    });
  });
  await expect(page.locator(".assistant-message")).toContainText(
    "Voice reply unavailable",
  );
  await expect(page.locator(".assistant-message details")).toContainText(
    "The verified full answer.",
  );
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.speech_text.delta", {
      response_id: "answer-1",
      turn_id: "voice-turn",
      phase: "answer",
      text: "The spoken ",
    });
    socket?.emit("portal.speech_text.done", {
      response_id: "answer-1",
      turn_id: "voice-turn",
      phase: "answer",
      text: "The spoken answer.",
    });
    socket?.emit("portal.audio.done", { response_id: "answer-1" });
  });
  await expect(page.locator(".assistant-message")).toContainText(
    "The spoken answer.",
  );
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.playback.clear", {});
  });
  await expect(page.locator(".assistant-message")).toContainText(
    "The spoken answer.",
  );
  secondTurn = true;
  await page.evaluate(() => {
    const socket = (
      window as typeof window & {
        __fakeVoiceSocket?: {
          emit(type: string, payload: Record<string, unknown>): void;
        };
      }
    ).__fakeVoiceSocket;
    socket?.emit("portal.transcript.done", {
      item_id: "input-2",
      text: "Find the actual product guide",
    });
    socket?.emit("portal.tool.started", {
      message: "Processing",
      user_text: "Find the actual product guide",
    });
  });
  await expect(page.locator(".turn")).toHaveCount(2);
  await expect(page.locator(".user-message")).toHaveCount(2);
  await expect(page.locator(".assistant-message").first()).toContainText(
    "The spoken answer.",
  );
});

test("Final SSE renders without a history round trip and survives an older pending response", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      if (String(input).endsWith("/events")) {
        const body = new ReadableStream({
          start(controller) {
            (window as any).__emitAnswerEvent = (
              type: string,
              payload: object,
            ) =>
              controller.enqueue(
                new TextEncoder().encode(
                  "data: " +
                    JSON.stringify({
                      type,
                      payload,
                      event_id: crypto.randomUUID(),
                      conversation_id: "latency-cid",
                      epoch: 0,
                      request_revision: 1,
                      server_seq: 1,
                      turn_id: "latency-turn",
                    }) +
                    "\n\n",
                ),
              );
          },
        });
        return new Response(body, {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return original(input, init);
    };
  });
  await page.route("**/api/v1/conversations", (route) =>
    route.fulfill({
      status: 201,
      json: {
        id: "latency-cid",
        title: "Test",
        epoch: 0,
        request_revision: 0,
        access_token: "fixture",
      },
    }),
  );
  let reads = 0;
  let hold = false;
  let release: (() => void) | undefined;
  await page.route(
    "**/api/v1/conversations/latency-cid/messages**",
    async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          json: { turn_id: "latency-turn", epoch: 0, request_revision: 1 },
        });
        return;
      }
      reads++;
      if (hold)
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      await route.fulfill({
        json: {
          epoch: 0,
          request_revision: 1,
          next_before: null,
          items: [
            {
              id: "latency-turn",
              user_text: "Find the guide",
              channel: "text",
              status: "running",
              answer: null,
              epoch: 0,
              request_revision: 1,
              parent_task_id: null,
              cancellation_reason: null,
              delivery_status: "pending_validation",
              output_suppressed: false,
            },
          ],
        },
      });
    },
  );
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("Find the guide");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".turn")).toContainText("Find the guide");
  await page
    .getByRole("textbox", { name: "Your question" })
    .fill("Another question");
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeEnabled();
  hold = true;
  await page.evaluate(() =>
    (window as any).__emitAnswerEvent("portal.tool.started", {
      message: "Searching",
    }),
  );
  await expect.poll(() => !!release).toBe(true);
  const beforeFinal = reads;
  await page.evaluate(() =>
    (window as any).__emitAnswerEvent("portal.answer.final", {
      answer_id: "answer-final",
      status: "answered",
      display_text: "The verified final answer",
      speech_text: "The verified final answer",
      citations: [],
      cards: [],
      is_mock: false,
      reason_code: null,
    }),
  );
  await expect(page.locator(".assistant-message")).toContainText(
    "The verified final answer",
  );
  expect(reads).toBe(beforeFinal);
  const lateResponse = page.waitForResponse((response) =>
    response.url().includes("/messages"),
  );
  release!();
  await lateResponse;
  await expect(page.locator(".assistant-message")).toContainText(
    "The verified final answer",
  );
  expect(reads).toBe(beforeFinal);
});
