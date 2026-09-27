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
