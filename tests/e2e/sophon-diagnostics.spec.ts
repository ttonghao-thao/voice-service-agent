import { expect, test } from "../../apps/web/node_modules/@playwright/test";
import type {
  DelegationObservation,
  InputObservation,
} from "../../apps/web/src/SophonInteraction";

test.use({
  launchOptions: {
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  },
  permissions: ["microphone"],
  viewport: { width: 390, height: 844 },
});

test("Sophon observations render without ASR and hide an open payload on access interruption", async ({
  page,
}) => {
  let cid = "";
  let messagesReleased = false;
  let releaseMessages = () => {};
  const blockMessages = new Promise<void>((resolve) => {
    releaseMessages = resolve;
  });
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  page.on("response", async (response) => {
    if (
      response.url().endsWith("/api/v1/conversations") &&
      response.request().method() === "POST"
    )
      cid = (await response.json()).id;
  });
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    const testWindow = window as typeof window & {
      testSendSSE?: (frame: string) => void;
      testEndSSE?: () => void;
    };
    window.fetch = (input, init) => {
      if (typeof input !== "string" || !input.endsWith("/events"))
        return original(input, init);
      const stream = new ReadableStream({
        start(controller) {
          let open = true;
          testWindow.testSendSSE = (frame) =>
            controller.enqueue(new TextEncoder().encode(frame));
          testWindow.testEndSSE = () => {
            if (!open) return;
            open = false;
            delete testWindow.testSendSSE;
            controller.close();
          };
          init?.signal?.addEventListener("abort", testWindow.testEndSSE, {
            once: true,
          });
        },
      });
      return Promise.resolve(
        new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        }),
      );
    };
  });
  await page.route("**/api/v1/conversations/*/messages*", async (route) => {
    if (!messagesReleased) await blockMessages;
    await route.fulfill({
      json: {
        items: [],
        records: [
          {
            kind: "sophon_interaction",
            epoch: 1,
            source_id: "spoken",
            server_seq: 1,
            created_at: "2026-11-28T00:00:00Z",
            payload: { ...input("spoken"), status: "awaiting_sophon" },
          },
        ],
        epoch: 1,
        request_revision: 0,
        next_before: null,
      },
    });
  });
  await page.routeWebSocket(
    "**/api/v1/voice-sessions/**/stream?*",
    (socket) => {
      socket.send(
        JSON.stringify({
          type: "portal.session.ready",
          event_id: "ready",
          conversation_id: cid,
          epoch: 1,
          server_seq: 1,
          request_revision: 0,
          turn_id: null,
          payload: {
            sample_rate: 24000,
            format: "pcm16",
            chunk_ms: 80,
            is_mock: true,
          },
        }),
      );
    },
  );

  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect.poll(() => cid).not.toEqual("");
  await expect
    .poll(() => page.evaluate(() => "testSendSSE" in window))
    .toBe(true);

  function input(id: string): InputObservation {
    return {
      kind: "input",
      interaction_id: id,
      input_item_id: id,
      voice_session_id: "test-sid",
      reason_code: null,
      status: "awaiting_sophon",
      observation_complete: false,
      candidate_reply: false,
    };
  }
  async function emit(
    serverSeq: number,
    payload: InputObservation | DelegationObservation,
  ) {
    const event = {
      type: "portal.sophon_interaction.updated",
      event_id: `diagnostic-${serverSeq}`,
      server_seq: serverSeq,
      conversation_id: cid,
      epoch: 1,
      request_revision: 0,
      turn_id: null,
      payload,
    };
    await page.evaluate(
      (frame) => {
        (
          window as unknown as { testSendSSE: (value: string) => void }
        ).testSendSSE(frame);
      },
      `id: ${serverSeq}\ndata: ${JSON.stringify(event)}\n\n`,
    );
  }
  await emit(1, input("spoken"));
  await expect(
    page.getByText("Awaiting Sophon", { exact: true }),
  ).toBeVisible();
  await emit(2, {
    ...input("spoken"),
    status: "sophon_replied",
    observation_complete: true,
    candidate_reply: true,
  });
  await emit(3, { ...input("delegated"), status: "delegation_received" });
  const attempt: DelegationObservation = {
    kind: "delegation",
    voice_session_id: "test-sid",
    input_item_id: "delegated",
    interaction_id: "attempt",
    delegation_id: "d001",
    status: "finished",
    reason_code: null,
    native_event: {
      type: "session.delegation.created",
      delegation: { task: "Check tomorrow's weather" },
    },
    structure: "valid",
    field_issues: [],
    redacted_fields: [],
    omitted_fields: 0,
    task_chars: 24,
    duplicates: 0,
    details_restricted: false,
    intent: {
      initial_route: "WEB_SEARCH",
      route: "WEB_SEARCH",
      parameters: { query: "Check tomorrow's weather" },
      selected_tool: "web_search",
      parse_method: "llm",
      router_model: "test-router",
      reason_code: null,
      policy_reason: null,
      parameters_redacted: false,
    },
    turn_id: "turn1",
    request_revision: 1,
    tool_executions: [
      {
        operation: "adapter",
        tool: "web_search",
        service: "Tavily",
        mode: "real",
        entered: true,
        status: "completed",
        duration_ms: 42,
        reason_code: null,
        result_status: "no_results",
      },
    ],
    result_status: "insufficient_evidence",
    result_reason: null,
    delivery: {
      kind: "tool_result",
      status: "receipt_unknown",
      reason_code: null,
    },
  };
  await emit(4, attempt);
  await emit(5, {
    ...attempt,
    interaction_id: "unassigned",
    input_item_id: null,
    delegation_id: null,
    structure: "invalid",
    native_event: {
      type: "session.delegation.created",
      delegation: { task: {} },
    },
    field_issues: [
      {
        path: "delegation.id",
        expected: "valid identifier",
        received: "missing",
        length: null,
      },
    ],
    intent: null,
    tool_executions: [],
    delivery: null,
    result_status: null,
  });
  messagesReleased = true;
  releaseMessages();
  await expect(page.locator(".user-message p")).toHaveCount(2);
  await expect(
    page.getByText("Sophon replied · no delegation observed", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Awaiting Sophon", { exact: true })).toHaveCount(
    0,
  );
  await page
    .locator("summary")
    .filter({ hasText: "Delegation details" })
    .press("Enter");
  await expect(
    page.getByText("Sophon payload", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.locator(".user-message pre").first()).toContainText(
    "Check tomorrow's weather",
  );
  await expect(
    page.getByText("Tool result · Gateway receipt unknown", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No usable result", { exact: true }),
  ).toBeVisible();
  const unassigned = page.getByRole("complementary", {
    name: "Unassigned Sophon events",
  });
  await unassigned
    .getByText("Invalid delegation structure", { exact: true })
    .click();
  await expect(
    unassigned.getByText("delegation.id", { exact: true }),
  ).toBeVisible();

  await page.evaluate(() =>
    (window as unknown as { testEndSSE: () => void }).testEndSSE(),
  );
  await expect(
    page.getByText("Payload access unavailable", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.locator(".user-message pre")).toHaveCount(0);
  expect(consoleErrors).toEqual([]);
});
