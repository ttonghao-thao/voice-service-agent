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

for (const outcome of [
  "voice_completed",
  "insufficient_evidence",
  "failed",
  "canceled",
]) {
  test(`Direct voice: evidence, ${outcome}, disabled text and monotonic recovery`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const cid = "direct-conversation";
    const citation = {
      citation_id: "C1",
      title: "Support guide",
      content: "Context supplied to voice.",
      context: null,
      context_parts: [],
      context_truncated: false,
      context_omitted: false,
      hits_omitted: 0,
      relations: [],
      version_id: "v1",
      business_version: null,
      updated_at: null,
      trace_id: "trace",
      retrieval_status: "ok",
      evidence_status: "unassessed",
      degraded_reasons: [],
      scope_limited: false,
      rank: 1,
      title_path: [],
      anchor: {},
      source_uri: null,
      is_mock: false,
    };
    const knowledge = {
      kind: "knowledge",
      result_id: "result",
      directive:
        outcome === "insufficient_evidence"
          ? "report_insufficient"
          : "answer_from_evidence",
      retrieval_status: "ok",
      evidence_status: "unassessed",
      citations: [citation],
      is_mock: false,
      reason_code: null,
      message: "",
    };
    const turn = {
      id: "turn",
      input_item_id: "input",
      created_at: "2026-10-03T00:00:00Z",
      user_text: "Find the product guide",
      channel: "voice",
      execution_mode: "direct",
      knowledge_result: null as unknown,
      status: "running",
      answer: null as unknown,
      epoch: 1,
      request_revision: 1,
      parent_task_id: null,
      cancellation_reason: null,
      delivery_status: "pending_validation",
      output_suppressed: false,
    };
    let visible = false;
    let revision = 1;
    const pendingSSE: object[] = [];
    let seq = 0;
    const event = (type: string, payload: object) => ({
      type,
      payload,
      event_id: `sse-${++seq}`,
      conversation_id: cid,
      epoch: 1,
      request_revision: 1,
      server_seq: seq,
      turn_id: "turn",
    });
    let sendWS: (type: string, payload: object) => void;
    await page.route("**/api/v1/capabilities", (route) =>
      route.fulfill({
        json: {
          execution_mode: "direct",
          external_llm_enabled: false,
          text_available: false,
          text_configured: false,
          is_mock: false,
          voice_available: true,
          agent_provider: "none",
          cuekb_mode: "real",
          weather_mode: "mock",
          provider: "nvidia",
          voice_session_max_seconds: 105,
        },
      }),
    );
    await page.route("**/api/v1/conversations", (route) =>
      route.fulfill({
        status: 201,
        json: {
          id: cid,
          title: "New conversation",
          epoch: 0,
          request_revision: 0,
          access_token: "fixture",
        },
      }),
    );
    await page.route(`**/api/v1/conversations/${cid}/voice-sessions`, (route) =>
      route.fulfill({
        status: 201,
        json: {
          voice_session_id: "sid",
          epoch: 1,
          request_revision: 0,
          ws_url: "/api/v1/voice-sessions/sid/stream?ticket=fixture",
        },
      }),
    );
    await page.route(`**/api/v1/conversations/${cid}/messages**`, (route) =>
      route.fulfill({
        json: {
          epoch: 1,
          request_revision: revision,
          items: visible ? [turn] : [],
          records: [],
          next_before: null,
        },
      }),
    );
    await page.route(`**/api/v1/conversations/${cid}/events`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: pendingSSE
          .splice(0)
          .map(
            (e: any) => `id: ${e.server_seq}\ndata: ${JSON.stringify(e)}\n\n`,
          )
          .join(""),
      }),
    );
    await page.route(
      `**/api/v1/conversations/${cid}/tasks/current/cancel`,
      (route) => {
        revision = 2;
        return route.fulfill({ json: { request_revision: 2, canceled: true } });
      },
    );
    await page.routeWebSocket(
      "**/api/v1/voice-sessions/**/stream?*",
      (socket) => {
        sendWS = (type, payload) =>
          socket.send(
            JSON.stringify({ ...event(type, payload), event_id: `ws-${seq}` }),
          );
        socket.onMessage((raw) => {
          if (JSON.parse(String(raw)).type === "portal.playback.stop")
            sendWS("portal.playback.clear", {});
        });
        socket.send(
          JSON.stringify({
            type: "portal.session.ready",
            conversation_id: cid,
            epoch: 1,
            request_revision: 0,
            event_id: "ready",
            payload: {
              sample_rate: 24000,
              format: "pcm16",
              chunk_ms: 80,
              is_mock: false,
            },
          }),
        );
      },
    );
    await page.goto("/");
    await expect(
      page.getByRole("textbox", { name: "Your question" }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Send question" }),
    ).toBeDisabled();
    await expect(
      page.getByText(
        "Text input is unavailable in this deployment. Please use voice.",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Start call", exact: true }).click();
    await expect(
      page.getByRole("main").getByText("Voice ready", { exact: true }),
    ).toBeVisible();
    sendWS!("portal.transcript.done", {
      item_id: "input",
      text: "Find the product guide",
    });
    await expect(page.locator(".user-message")).toContainText(
      "Find the product guide",
    );
    visible = true;
    pendingSSE.push(
      event("portal.tool.started", {
        message: "Searching",
        user_text: turn.user_text,
      }),
    );
    await expect(page.locator(".assistant-message")).toContainText("Searching");
    pendingSSE.push(event("portal.knowledge.ready", knowledge));
    await expect(page.locator(".assistant-message")).toContainText(
      "Preparing voice reply",
    );
    await expect(
      page.getByRole("button", { name: "Cancel search" }),
    ).toBeEnabled();
    await expect(page.locator(".assistant-message details")).toHaveCount(0);
    await expect(page.locator(".assistant-message")).not.toContainText(
      citation.content,
    );
    await page
      .getByRole("button", { name: "Stop playback", exact: true })
      .click();
    await expect(page.locator(".assistant-message")).toContainText(
      "Preparing voice reply",
    );
    if (outcome === "canceled") {
      turn.status = "canceled";
      await page.getByRole("button", { name: "Cancel search" }).click();
      await expect(page.locator(".assistant-message")).toContainText(
        "Canceled",
      );
    } else {
      sendWS!("portal.speech_text.done", {
        response_id: "answer",
        phase: "answer",
        segment_index: 0,
        text: "Actual voice reply.",
      });
      await expect(page.locator(".assistant-message")).toContainText(
        "Actual voice reply.",
      );
      const answer = {
        answer_id: "answer",
        status: outcome,
        answer_origin: "voicechat",
        evidence_role: "retrieved_context",
        display_text:
          outcome === "failed"
            ? "Voice reply is unavailable."
            : "Actual voice reply.",
        speech_text: "Actual voice reply.",
        citations: [citation],
        cards: [],
        is_mock: false,
        reason_code: outcome === "failed" ? "VOICE_ANSWER_TIMEOUT" : null,
      };
      pendingSSE.push(event("portal.answer.final", answer));
      await expect(page.locator(".assistant-message")).toContainText(
        outcome === "voice_completed"
          ? "Response completed"
          : outcome === "failed"
            ? "Voice reply unavailable"
            : "Insufficient evidence",
      );
      await expect(
        page
          .locator(".assistant-message p")
          .filter({ hasText: "Actual voice reply." }),
      ).toHaveCount(1);
      pendingSSE.push(event("portal.knowledge.ready", knowledge));
      sendWS!("portal.playback.clear", {}); // forces an intentionally old running snapshot
      await expect(page.locator(".assistant-message")).not.toContainText(
        "Preparing voice reply",
      );
      await expect(page.locator(".assistant-message details")).toHaveCount(0);
      await page
        .locator(".assistant-message")
        .getByRole("button", { name: /Sources consulted/ })
        .click();
      await expect(page.locator(".ant-drawer")).toContainText(
        "Retrieved context supplied for the spoken reply",
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
  });
}
