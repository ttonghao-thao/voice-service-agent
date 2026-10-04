import { test, expect } from "../../apps/web/node_modules/@playwright/test";

// Opt in explicitly: this test requires scripts/simulate_full_flow.py --serve.
test.skip(
  process.env.SIMULATED_FULL_FLOW !== "1",
  "Requires the independent API simulation harness",
);

test("Independent API flow: general answer, direct evidence, external reasoning and cleanup", async ({
  page,
}) => {
  const errors: string[] = [];
  const frames: Record<string, unknown>[] = [];
  let conversation: { id: string; access_token: string };
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", async (response) => {
    if (
      response.url().endsWith("/api/v1/conversations") &&
      response.request().method() === "POST"
    ) {
      conversation = await response.json();
    }
  });
  page.on("websocket", (socket) =>
    socket.on("framereceived", ({ payload }) => {
      try {
        frames.push(JSON.parse(String(payload)));
      } catch {
        /* Binary browser frames are irrelevant here. */
      }
    }),
  );
  // Exercise capture, resampling, AudioWorklet and transport with a synthetic tone.
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const oscillator = context.createOscillator();
      const destination = context.createMediaStreamDestination();
      oscillator.frequency.value = 500;
      oscillator.connect(destination);
      oscillator.start();
      (
        window as typeof window & { __simulationMicrophone?: AudioContext }
      ).__simulationMicrophone = context;
      return destination.stream;
    };
  });
  const beforeResponse = await page.request.get(
    "http://127.0.0.1:8000/__simulation/state",
  );
  expect(beforeResponse.ok()).toBe(true);
  const before = await beforeResponse.json();
  expect(before).toMatchObject({ simulation: true, real_service: false });

  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(
    page.getByRole("main").getByText("Voice ready", { exact: true }),
  ).toBeVisible();
  const questions = [
    {
      question: "What is MLO?",
      tool: null,
      spoken: "MLO means multi-link operation.",
    },
    {
      question: "How many connections does Product AX support?",
      tool: "lookup_knowledge",
      spoken: "Product AX supports 10 connections.",
    },
    {
      question: "Explain the connection limit in the AX documentation",
      tool: "reason_over_knowledge",
      spoken: "Product AX supports 10 connections.",
    },
  ];
  for (const [index, scenario] of questions.entries()) {
    const response = await page.request.post(
      "http://127.0.0.1:8000/__simulation/plan",
      { data: scenario },
    );
    expect(response.ok()).toBe(true);
    const plan = await response.json();
    await expect(page.locator(".user-message").nth(index)).toContainText(
      scenario.question,
    );
    await expect(page.locator(".assistant-message").nth(index)).toContainText(
      scenario.spoken,
    );
    await expect
      .poll(() =>
        frames.some(
          (event) =>
            event.type === "portal.audio.done" &&
            (event.payload as { response_id?: string })?.response_id ===
              plan.response_id,
        ),
      )
      .toBe(true);
    if (scenario.tool) {
      const details = page
        .locator(".assistant-message")
        .nth(index)
        .locator("details");
      await expect(details).toBeVisible();
      await details.locator("summary").click();
      await details.getByRole("button", { name: /View sources/ }).click();
      await expect(
        page.getByRole("dialog").locator(".citation-title"),
      ).toContainText("C1");
      await expect(page.getByRole("dialog").locator(".citation")).toContainText(
        "Product AX supports 10 connections.",
      );
      await page
        .getByRole("dialog")
        .getByRole("button", { name: "Close", exact: true })
        .click();
      if (scenario.tool === "lookup_knowledge") {
        await expect(details).toContainText(
          "Sources checked after the spoken response.",
        );
      }
    } else {
      await expect(page.locator(".assistant-message").first()).toContainText(
        "Model general answer",
      );
      await expect(page.locator(".assistant-message").first()).toContainText(
        "No company sources were checked.",
      );
    }
    const state = await (
      await page.request.get("http://127.0.0.1:8000/__simulation/state")
    ).json();
    expect(state.queries - before.queries).toBe(index);
    expect(state.model_calls - before.model_calls).toBe(index === 2 ? 2 : 0);
    expect(state.native_results - before.native_results).toBe(index);
    if (index === 1) {
      await page
        .getByRole("button", { name: "Stop playback", exact: true })
        .click();
      await expect(page.locator(".assistant-message").nth(index)).toContainText(
        scenario.spoken,
      );
    }
  }
  const token = { Authorization: "Bearer " + conversation!.access_token };
  const history = await (
    await page.request.get(
      `/api/v1/conversations/${conversation!.id}/messages`,
      { headers: token },
    )
  ).json();
  expect(
    history.items.map((turn: { selected_tool: string }) => turn.selected_tool),
  ).toEqual(["lookup_knowledge", "reason_over_knowledge"]);
  expect(
    history.items.every(
      (turn: { answer: { status: string } }) =>
        turn.answer.status === "answered",
    ),
  ).toBe(true);
  const ended = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/conversations/${conversation!.id}`) &&
      response.request().method() === "DELETE",
  );
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect((await ended).status()).toBe(200);
  await expect(
    page.getByRole("main").getByText("Voice disconnected", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".assistant-message")).toHaveCount(3);
  const revoked = await page.request.get(
    `/api/v1/conversations/${conversation!.id}/messages`,
    { headers: token },
  );
  expect(revoked.status()).toBe(401);
  expect(errors).toEqual([]);
});
