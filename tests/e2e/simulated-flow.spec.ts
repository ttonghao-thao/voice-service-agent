import { test, expect } from "../../apps/web/node_modules/@playwright/test";

const mode = process.env.SIMULATION_MODE;
const voiceURL = process.env.SIMULATION_VOICE_URL;
const cuekbURL = process.env.SIMULATION_CUEKB_URL;
const llmURL = process.env.SIMULATION_LLM_URL;
const question = "Find the synthetic AX guide for software version 3.2.";
const spoken =
  "The synthetic AX guide says to disconnect power before servicing.";

test.skip(
  !voiceURL || !cuekbURL || !llmURL,
  "Run through scripts/test_simulated_flow.py",
);

test.beforeEach(async ({ request }) => {
  for (const url of [voiceURL, cuekbURL, llmURL]) {
    const response = await request.delete(`${url}/__test__/requests`);
    expect(response.ok()).toBeTruthy();
  }
  await request.post(`${voiceURL}/__test__/scenario`, { data: {} });
});

test("Network voice flow renders ASR, actual speech, sources and ends the call", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  const frames: any[] = [];
  await page.addInitScript(() => {
    const nativeConnect = AudioNode.prototype.connect;
    AudioWorkletNode.prototype.connect = function (destination: AudioNode) {
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 256;
      nativeConnect.call(this, analyser);
      nativeConnect.call(analyser, destination);
      const scope = window as typeof window & {
        __maxRms?: number;
        __readRms?: () => number;
      };
      scope.__readRms = () => {
        const values = new Float32Array(analyser.fftSize);
        analyser.getFloatTimeDomainData(values);
        return Math.sqrt(
          values.reduce((sum, value) => sum + value * value, 0) / values.length,
        );
      };
      const observe = () => {
        scope.__maxRms = Math.max(
          scope.__maxRms || 0,
          scope.__readRms?.() || 0,
        );
        requestAnimationFrame(observe);
      };
      requestAnimationFrame(observe);
      return destination;
    } as typeof AudioWorkletNode.prototype.connect;
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("websocket", (socket) => {
    socket.on("framereceived", (frame) => {
      frames.push(JSON.parse(String(frame.payload)));
    });
  });
  await page.goto("/");
  const text = page.getByRole("textbox", { name: "Your question" });
  if (mode === "direct") await expect(text).toBeDisabled();
  else await expect(text).toBeEnabled();
  const callResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/conversations") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  const call = await (await callResponse).json();
  await expect(page.locator(".user-message")).toContainText(question);
  await expect(page.locator(".assistant-message")).toContainText(spoken);
  await expect(page.locator(".assistant-message")).toContainText(
    mode === "direct" ? "Response completed" : "Answered",
  );
  await expect
    .poll(() =>
      frames.some(
        (event) =>
          event.type === "portal.audio.delta" &&
          event.payload.response_id === "answer-0",
      ),
    )
    .toBeTruthy();
  await expect
    .poll(() =>
      frames.some(
        (event) =>
          event.type === "portal.speech_text.done" &&
          event.payload.text === spoken,
      ),
    )
    .toBeTruthy();
  await expect
    .poll(() => page.evaluate(() => (window as any).__maxRms || 0))
    .toBeGreaterThan(0.02);
  await page
    .getByRole("button", { name: "Stop playback", exact: true })
    .click();
  await expect
    .poll(() => page.evaluate(() => (window as any).__readRms?.() || 0))
    .toBeLessThan(0.001);
  await expect(page.locator(".assistant-message")).toContainText(spoken);
  if (mode === "direct") {
    await page.getByRole("button", { name: /Sources consulted/ }).click();
  } else {
    await page.getByText("View full answer / Sources").click();
    await page
      .locator(".assistant-message")
      .getByRole("button", { name: /sources/i })
      .click();
  }
  await expect(page.locator(".ant-drawer")).toContainText(
    "Synthetic AX support guide",
  );
  await expect(page.locator(".ant-drawer")).toContainText(
    "disconnect power before servicing",
  );
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: `../../artifacts/simulation/${mode}-portal.png`,
    fullPage: true,
  });
  const upstream = await (
    await request.get(`${voiceURL}/__test__/requests`)
  ).json();
  expect(
    upstream.some(
      (r: any) => r.type === "input_audio_buffer.append" && r.bytes === 3840,
    ),
  ).toBeTruthy();
  expect(
    upstream.filter((r: any) => r.type === "conversation.item.create"),
  ).toHaveLength(1);
  const kb = await (await request.get(`${cuekbURL}/__test__/requests`)).json();
  expect(kb).toHaveLength(1);
  expect(kb[0].query).toBe(question);
  const llm = await (await request.get(`${llmURL}/__test__/requests`)).json();
  expect(llm).toHaveLength(mode === "direct" ? 0 : 2);
  const ended = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/conversations/${call.id}`) &&
      response.request().method() === "DELETE",
  );
  await page.getByRole("button", { name: "End call", exact: true }).click();
  expect((await ended).status()).toBe(200);
  const revoked = await request.get(
    `http://127.0.0.1:8000/api/v1/conversations/${call.id}/messages`,
    {
      headers: { Authorization: `Bearer ${call.access_token}` },
    },
  );
  expect([401, 403, 404]).toContain(revoked.status());
  await page.reload();
  await expect(page.locator(".assistant-message")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("Missing evidence remains insufficient through the network and UI", async ({
  page,
  request,
}) => {
  await request.post(`${voiceURL}/__test__/scenario`, {
    data: { questions: ["Find no evidence"] },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  await expect(page.locator(".user-message")).toContainText("Find no evidence");
  await expect(page.locator(".assistant-message")).toContainText(
    "Insufficient evidence",
  );
  await expect(page.locator(".assistant-message")).toContainText(
    "No matching synthetic evidence",
  );
  await expect(page.locator(".citation-title")).toHaveCount(0);
  await page.getByRole("button", { name: "End call", exact: true }).click();
});

test("Cancel search fences delayed independent API results", async ({
  page,
  request,
}) => {
  await request.post(`${voiceURL}/__test__/scenario`, {
    data: { questions: ["Find slow synthetic knowledge"] },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start call", exact: true }).click();
  const cancel = page.getByRole("button", {
    name: "Cancel search",
    exact: true,
  });
  await expect(cancel).toBeEnabled();
  await cancel.click();
  await expect(page.locator(".assistant-message")).toContainText("Canceled");
  // Wait for the upstream delayed response; a stale answer must not appear.
  await expect
    .poll(async () => {
      const events = await (
        await request.get(`${voiceURL}/__test__/requests`)
      ).json();
      return events.some((r: any) => r.type === "closed");
    })
    .toBeTruthy();
  await expect(page.locator(".assistant-message")).not.toContainText(spoken);
  expect(
    (await (await request.get(`${voiceURL}/__test__/requests`)).json()).filter(
      (r: any) => r.type === "conversation.item.create",
    ),
  ).toHaveLength(0);
  await page.getByRole("button", { name: "End call", exact: true }).click();
});

test("Mode-specific text entry uses the actual API and streaming SDK path", async ({
  page,
  request,
}) => {
  await page.goto("/");
  const text = page.getByRole("textbox", { name: "Your question" });
  if (mode === "direct") {
    await expect(text).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Send question" }),
    ).toBeDisabled();
    expect(
      await (await request.get(`${llmURL}/__test__/requests`)).json(),
    ).toHaveLength(0);
    return;
  }
  await text.fill(question);
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".assistant-message")).toContainText(spoken);
  await expect(page.locator(".citation-title")).toContainText("C1");
  const llm = await (await request.get(`${llmURL}/__test__/requests`)).json();
  expect(llm).toHaveLength(2);
  expect(llm.every((r: any) => r.stream === true)).toBeTruthy();
});
