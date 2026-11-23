// Real browser file-input acceptance. No API or model responses are mocked.
import { createRequire } from "node:module";
import { parseArgs } from "node:util";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(join(root, "apps/web/package.json"));
const { chromium } = require("@playwright/test");
const { values } = parseArgs({ options: {
  cases: { type: "string" }, output: { type: "string" },
  "base-url": { type: "string", default: "http://localhost:5173" },
  language: { type: "string" }, only: { type: "string" },
  "server-log": { type: "string" }, "source-revision": { type: "string" },
  "observe-seconds": { type: "string", default: "20" },
  "asr-state": { type: "string", default: "unknown" },
  "profile-json": { type: "string" },
  "local-ice": { type: "boolean", default: false },
} });
if (!values.cases || !values.output) throw new Error("Provide --cases cases.json and --output /absolute/report/directory");
const output = resolve(values.output);
const observe = Number(values["observe-seconds"]);
if (!["unknown", "enabled", "disabled"].includes(values["asr-state"])) throw new Error("Invalid --asr-state");
if (!Number.isFinite(observe) || observe < 14 || observe > 80) throw new Error("Observation must be 14–80 seconds");
if (values["local-ice"] && !["localhost", "127.0.0.1", "[::1]"].includes(new URL(values["base-url"]).hostname)) {
  throw new Error("--local-ice is only permitted for a loopback test application");
}
await mkdir(join(output, "inputs"), { recursive: true });
let cases = JSON.parse(await readFile(resolve(values.cases), "utf8"));
if (values.language) cases = cases.filter((c) => c.lang === values.language);
if (values.only) cases = cases.filter((c) => values.only.split(",").includes(c.id));
if (!cases.length) throw new Error("No cases selected");
const browserArgs = ["--autoplay-policy=no-user-gesture-required", ...(values["local-ice"]
  ? ["--allow-loopback-in-peer-connection", "--disable-features=WebRtcHideLocalIpsWithMdns",
    "--force-webrtc-ip-handling-policy=default"] : [])];
const browser = await chromium.launch({ headless: true,
  args: browserArgs,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}),
});
const results = [];
const voiceProfile = values["profile-json"] ? JSON.parse(await readFile(resolve(values["profile-json"]), "utf8")) : null;
const sourceFiles = ["apps/api/app/voice/livekit_room.py", "apps/api/app/voice/delegation_router.py",
  "apps/api/app/config.py", "apps/api/app/tools/registry.py", "config/delegation-router-prompt.txt",
  "apps/api/app/voice/profiles.py", "apps/api/app/voice/sophon_gpt_live.py",
  "apps/api/app/voice/gateway.py", "apps/api/app/api/routes.py", "apps/web/src/App.tsx", "apps/web/src/VoiceProfiles.tsx",
  "apps/web/src/audio/VoiceClient.ts", "apps/web/src/audio/LiveKitMedia.ts",
  "config/sophon-livekit-prompt-zh.txt", "config/sophon-livekit-prompt-en.txt"];
const fileHashes = Object.fromEntries(await Promise.all(sourceFiles.map(async (name) =>
  [name, createHash("sha256").update(await readFile(join(root, name))).digest("hex")])));
try {
  for (const c of cases) {
    if (!/^[a-z0-9_]+$/.test(c.id) || !["delegate", "none"].includes(c.expect)) throw new Error("Invalid official case entry");
    const original = resolve(dirname(resolve(values.cases)), `${c.id}.wav`);
    const input = join(output, "inputs", `${c.id}.wav`);
    execFileSync("ffmpeg", ["-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", original,
      "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", "-map_metadata", "-1", input]);
    const r = { id: c.id, language: c.lang, expected_delegation: c.expect, text: c.text,
      expected_route: c.route || ({ zh_weather: "WEATHER", en_weather: "WEATHER", zh_news: "WEB_SEARCH" }[c.id] ?? null),
      asr_enabled: values["asr-state"] === "unknown" ? null : values["asr-state"] === "enabled",
      original_sha256: createHash("sha256").update(await readFile(original)).digest("hex"),
      input_sha256: createHash("sha256").update(await readFile(input)).digest("hex"),
      events: [], diagnostics: [], page_errors: [], control_pcm_frames: 0,
      observation_complete: false, answer_complete: "unverified" };
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
      // File input still uses the selected WAV. This local-only permission lets
      // Chromium expose LAN ICE candidates when a host VPN owns the default route.
      permissions: values["local-ice"] ? ["microphone"] : [],
    });
    const page = await context.newPage();
    let accessToken = ""; // In memory only; never put call credentials in a report.
    await page.addInitScript(() => {
      const state = window.__voiceAcceptance = { source_start_ms: null, speech_end_ms: null,
        first_active_audio_ms: null, last_active_audio_ms: null, active_frames: 0, received_frames: 0,
        output_rate: null, input_duration_ms: null, chunks: [] };
      const start = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function (...args) {
        if (this.buffer && state.source_start_ms === null) {
          state.source_start_ms = performance.now();
          state.input_duration_ms = this.buffer.duration * 1000;
          const samples = this.buffer.getChannelData(0), width = Math.round(this.buffer.sampleRate * .02);
          let last = samples.length;
          for (let offset = 0; offset + width <= samples.length; offset += width) {
            let power = 0;
            for (let i = offset; i < offset + width; i++) power += samples[i] * samples[i];
            if (Math.sqrt(power / width) > 300 / 32768) last = offset + width;
          }
          state.speech_end_ms = state.source_start_ms + last / this.buffer.sampleRate * 1000;
        }
        return start.apply(this, args);
      };
      const descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
      Object.defineProperty(HTMLMediaElement.prototype, "srcObject", { ...descriptor, set(stream) {
        descriptor.set.call(this, stream);
        if (!(stream instanceof MediaStream) || !stream.getAudioTracks().length || state.output_rate) return;
        const audio = new AudioContext();
        state.output_rate = audio.sampleRate;
        const source = audio.createMediaStreamSource(stream);
        const processor = audio.createScriptProcessor(1024, 1, 1);
        const mute = audio.createGain(); mute.gain.value = 0;
        source.connect(processor); processor.connect(mute); mute.connect(audio.destination);
        processor.onaudioprocess = (event) => {
          const samples = event.inputBuffer.getChannelData(0);
          const chunk = new Int16Array(samples.length);
          let peak = 0;
          for (let i = 0; i < samples.length; i++) {
            chunk[i] = Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767);
            peak = Math.max(peak, Math.abs(chunk[i]));
          }
          state.received_frames++;
          if (state.source_start_ms !== null && state.chunks.length < 6000) state.chunks.push(chunk);
          if (state.speech_end_ms !== null && performance.now() >= state.speech_end_ms && peak > 600) {
            state.first_active_audio_ms ??= performance.now();
            state.last_active_audio_ms = performance.now(); state.active_frames++;
          }
        };
        void audio.resume();
      } });
    });
    page.on("pageerror", (e) => r.page_errors.push(e.message));
    page.on("console", (message) => {
      if (!message.text().startsWith("voice_file ")) return;
      try { r.diagnostics.push(JSON.parse(message.text().slice(11))); } catch {}
    });
    page.on("response", async (response) => {
      if (response.request().method() === "POST" && new URL(response.url()).pathname === "/api/v1/conversations") {
        try { const call = await response.json(); r.conversation_id = call.id; accessToken = call.access_token; } catch {}
      }
    });
    page.on("websocket", (wire) => {
      if (!new URL(wire.url()).pathname.startsWith("/api/")) return;
      wire.on("framereceived", ({ payload }) => {
        try { const e = JSON.parse(String(payload)); r.events.push({ at: Date.now(), type: e.type, payload: e.payload }); } catch {}
      });
      wire.on("framesent", ({ payload }) => {
        try { if (JSON.parse(String(payload)).type === "portal.audio.append") r.control_pcm_frames++; } catch {}
      });
    });
    try {
      await page.goto(values["base-url"]);
      r.capabilities = await page.evaluate(async () => (await fetch("/api/v1/capabilities")).json());
      if (r.capabilities.is_mock || r.capabilities.voice_transport !== "livekit") throw new Error("Requires a real-provider LiveKit application");
      if (voiceProfile) {
        await page.getByRole("button", { name: "Voice settings", exact: true }).click();
        await page.getByRole("dialog").waitFor();
        await page.getByRole("button", { name: "New profile", exact: true }).click();
        await page.getByLabel("Profile name", { exact: true }).fill(voiceProfile.name);
        await page.getByLabel("Profile description").fill(voiceProfile.description || "");
        await page.getByLabel("Language style").fill(voiceProfile.language_style || "");
        await page.getByRole("button", { name: "Save profile", exact: true }).click();
        await page.getByRole("button", { name: "Delete profile", exact: true }).waitFor({ state: "visible" });
        await page.waitForFunction(() => !Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Delete profile")?.disabled);
        r.voice_profile = { name: voiceProfile.name, description: voiceProfile.description || "", language_style: voiceProfile.language_style || "" };
        await page.getByRole("button", { name: "Close", exact: true }).click();
      }
      await page.getByText("Audio file", { exact: true }).click();
      await page.getByLabel("Recorded WAV file").setInputFiles(input);
      await page.getByRole("button", { name: "Start file call", exact: true }).click();
      await page.getByRole("main").getByText("Voice ready", { exact: true }).waitFor({ timeout: 25000 });
      await page.waitForFunction(() => window.__voiceAcceptance.source_start_ms !== null, null, { timeout: 10000 });
      const duration = await page.evaluate(() => window.__voiceAcceptance.input_duration_ms);
      await page.waitForTimeout(duration + observe * 1000);
      r.observation_complete = true;
    } catch (e) { r.error = String(e.message).split("\n")[0]; }
    try {
      r.media = await page.evaluate(() => {
        const { chunks, ...metrics } = window.__voiceAcceptance;
        const pcm = new Int16Array(chunks.reduce((sum, c) => sum + c.length, 0));
        let offset = 0;
        for (const chunk of chunks) { pcm.set(chunk, offset); offset += chunk.length; }
        let binary = "";
        const bytes = new Uint8Array(pcm.buffer);
        for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
        const element = document.querySelector("audio");
        return { ...metrics, captured_at_ms: performance.now(), pcm: btoa(binary), element: element ? { muted: element.muted, paused: element.paused, current_time: element.currentTime } : null };
      });
      const pcm = Buffer.from(r.media.pcm, "base64"); delete r.media.pcm;
      const header = Buffer.alloc(44);
      header.write("RIFF"); header.writeUInt32LE(pcm.length + 36, 4); header.write("WAVEfmt ", 8);
      header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
      header.writeUInt32LE(r.media.output_rate || 48000, 24); header.writeUInt32LE((r.media.output_rate || 48000) * 2, 28);
      header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34); header.write("data", 36); header.writeUInt32LE(pcm.length, 40);
      await writeFile(join(output, `${c.id}-reply.wav`), Buffer.concat([header, pcm]));
      r.first_active_audio_after_speech_ms = r.media.first_active_audio_ms === null ? null : Math.round(r.media.first_active_audio_ms - r.media.speech_end_ms);
      r.input_transcript = r.events.filter((e) => e.type === "portal.transcript.delta").map((e) => e.payload.text).join("");
      r.output_transcript = r.events.filter((e) => e.type === "portal.speech_text.delta").map((e) => e.payload.text).join("");
      r.page_text = await page.locator("main").innerText();
      r.output_caption_visible = !!r.output_transcript && r.page_text.replace(/\s+/g, " ").includes(r.output_transcript.replace(/\s+/g, " "));
      r.window_ended_during_audio = r.media.last_active_audio_ms !== null && r.media.captured_at_ms - r.media.last_active_audio_ms < 1000;
      if (r.conversation_id && accessToken) {
        const history = await page.evaluate(async ({ id, token }) => {
          const response = await fetch(`/api/v1/conversations/${id}/messages`, { headers: { Authorization: `Bearer ${token}` } });
          if (!response.ok) throw new Error("Acceptance history unavailable");
          return response.json();
        }, { id: r.conversation_id, token: accessToken });
        r.route_records = history.records.filter((item) => item.kind === "sophon_route");
        r.business_outcomes = history.items.map((item) => ({ delegation_id: item.native_call_id,
          tool: item.selected_tool, status: item.answer?.status ?? item.status,
          reason_code: item.answer?.reason_code ?? null }));
      }
      await page.screenshot({ path: join(output, `${c.id}.png`), fullPage: true });
      const end = page.getByRole("button", { name: "End call", exact: true });
      if (await end.isVisible()) await end.click();
    } catch (e) { r.capture_error = String(e.message).split("\n")[0]; }
    await context.close();
    if (values["server-log"] && r.conversation_id) {
      r.server_events = (await readFile(values["server-log"], "utf8")).split("\n").filter((line) => line.includes(`conversation_id=${r.conversation_id}`));
      const raw = r.server_events.filter((line) => line.includes("sophon_room_delegation_received "));
      r.delegations = [...new Set(raw.map((line) => line.match(/delegation_id=(\S+)/)?.[1]).filter(Boolean))];
      r.delegation_matches = r.observation_complete && ((r.delegations.length > 0) === (c.expect === "delegate"));
      r.tool_runs = r.server_events.filter((line) => line.includes("tool_run_finished "));
      r.gateway_received_results = r.server_events.filter((line) => line.includes("sophon_room_delegation ") && line.includes("outcome=VOICE_ANSWER_UNVERIFIED")).length;
      r.raw_input_transcript_events = r.server_events.filter((line) => line.includes("sophon_room_input_transcript ")).length;
      r.native_delegation = r.delegations.length > 0;
      r.delegation_tasks = (r.route_records || []).map((item) => ({ delegation_id: item.source_id, ...item.payload }));
      r.route = r.delegation_tasks[0]?.route ?? null;
      r.parse_method = r.delegation_tasks[0]?.parse_method ?? null;
      r.route_matches = r.expected_route && r.native_delegation ? r.route === r.expected_route : null;
      r.tool_executed = r.tool_runs.length > 0;
      r.tool_status = r.tool_runs.map((line) => line.match(/status=(\S+)/)?.[1] ?? "unknown");
      r.commentary_ack = r.server_events.some((line) => line.includes("sophon_room_commentary_ack "));
    }
    r.asr_text = r.input_transcript;
    r.speech_observed = r.media?.active_frames > 0;
    r.final_answer_verified = false;
    r.provider_errors = r.events.filter((e) => e.type === "portal.error").map((e) => e.payload);
    r.media_transport_ok = r.observation_complete && r.media?.active_frames > 0 && r.control_pcm_frames === 0
      && r.page_errors.length === 0 && r.provider_errors.length === 0;
    results.push(r);
    await writeFile(join(output, `${c.id}.json`), JSON.stringify(r, null, 2));
    console.log(JSON.stringify({ id: r.id, observed: r.observation_complete, delegated: r.delegations?.length,
      delegation_matches: r.delegation_matches, active_audio: r.media?.active_frames > 0,
      first_active_audio_ms: r.first_active_audio_after_speech_ms, input_transcript: r.input_transcript,
      output_transcript: r.output_transcript, tool_runs: r.tool_runs, error: r.error }));
  }
} finally { await browser.close(); }
await writeFile(join(output, "summary.json"), JSON.stringify({ application_commit: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
  application_files_sha256: fileHashes,
  browser_args: browserArgs, local_ice: values["local-ice"],
  browser_permissions: values["local-ice"] ? ["microphone"] : [],
  source_revision: values["source-revision"] || null,
  metrics: {
    expected_native_cases: results.filter((r) => r.expected_delegation === "delegate").length,
    triggered_expected_native_cases: results.filter((r) => r.expected_delegation === "delegate" && r.native_delegation === true).length,
    missed_expected_native_cases: results.filter((r) => r.expected_delegation === "delegate" && r.native_delegation === false).map((r) => r.id),
    unexpected_native_cases: results.filter((r) => r.expected_delegation === "none" && r.native_delegation === true).map((r) => r.id),
    supported_native_cases: results.filter((r) => r.expected_route && r.native_delegation).length,
    correctly_routed_supported_cases: results.filter((r) => r.route_matches === true).length,
    llm_parsed_cases: results.filter((r) => r.parse_method === "llm").length,
    llm_failure_cases: results.filter((r) => r.delegation_tasks?.some((task) => task.reason?.startsWith("llm_"))).map((r) => r.id),
    tools_executed: results.filter((r) => r.tool_executed).length,
    tools_succeeded: results.filter((r) => r.tool_executed && r.tool_status.every((s) => s === "ok")).length,
    business_answered_cases: results.filter((r) => r.business_outcomes?.some((item) => item.status === "answered")).length,
    tool_result_gateway_receipts: results.filter((r) => r.gateway_received_results > 0).length,
    candidate_speech_cases: results.filter((r) => r.speech_observed).length,
    final_answer_verified_cases: 0,
    incomplete_observation_cases: results.filter((r) => !r.observation_complete || r.native_delegation === undefined).map((r) => r.id),
  }, cases: results }, null, 2));
