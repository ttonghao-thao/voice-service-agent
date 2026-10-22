import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { PlaybackRing, Resampler, pcm16 } from "../public/dsp.js";
import { loadRecordingModule, recordingWave } from "./recording-fixtures.mjs";

const recordingModule = loadRecordingModule();
function harness(rate = 48000) {
  const exports = {},
    messages = [],
    errors = [],
    progress = [];
  let socket,
    clock = 1000,
    options,
    processor,
    port,
    context;
  const apiCalls = [], diagnostics = [], portalEvents = [];
  const listeners = new Map();
  const document = { hidden: false, addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name) => listeners.delete(name) };
  let Processor;
  const worklet = readFileSync(new URL("../public/audio-worklet.js", import.meta.url), "utf8")
    .replace(/^import .*;\n/, "");
  vm.runInNewContext(worklet, { Resampler, PlaybackRing, pcm16, sampleRate: rate,
    AudioWorkletProcessor: class { port = { postMessage: (data) => port?.onmessage?.({ data }) }; },
    registerProcessor: (_name, implementation) => { Processor = implementation; } });
  const source = ts.transpileModule(
    readFileSync(
      new URL("../src/audio/VoiceClient.ts", import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    bufferedAmount = 0;
    constructor() {
      socket = this;
    }
    send(raw) {
      messages.push(JSON.parse(raw));
    }
    close() {
      this.readyState = 3;
    }
    emit(type, payload = {}) {
      this.onmessage({ data: JSON.stringify({ type, epoch: 1, payload }) });
    }
  }
  vm.runInNewContext(source, {
    exports,
    require: () => ({ api: async (path, options) => {
      apiCalls.push({ path, options });
      return { voice_session_id: "sid", epoch: 1, request_revision: 0, ws_url: "/stream" };
    } }),
    AudioContext: class {
      state = "running";
      resumes = 0;
      constructor() { context = this; }
      audioWorklet = { addModule: async () => {} };
      resume = async () => { this.resumes++; this.state = "running"; };
      close = async () => {};
      // No createBuffer/BufferSource: files must not enter the resampler.
    },
    AudioWorkletNode: class {
      connect() {}
      disconnect() {}
      constructor(_context, _name, config) {
        options = config;
        processor = new Processor(config);
        this.port = port = { postMessage: (data) => processor.port.onmessage({ data }) };
      }
    },
    WebSocket: Socket,
    URL,
    location: { href: "https://example.test", protocol: "https:" },
    document,
    window: { addEventListener() {}, removeEventListener() {} },
    performance: { now: () => clock },
    crypto: { randomUUID: () => "event" },
    btoa,
    console: { info(_label, raw) { diagnostics.push(JSON.parse(raw)); } },
    setTimeout: () => { throw new Error("File audio must not depend on window timers"); },
  });
  const client = new exports.VoiceClient(
    (event) => portalEvents.push(event),
    () => {},
    (e) => errors.push(e),
    () => {},
    (p) => progress.push(p),
  );
  return {
    client,
    messages,
    errors,
    progress,
    apiCalls,
    diagnostics,
    portalEvents,
    context: () => context,
    visibility(hidden) { document.hidden = hidden; listeners.get("visibilitychange")?.(); },
    frame(data) { port.onmessage({ data }); },
    socket: () => socket,
    options: () => options,
    tick(ms = 80) {
      clock += ms;
      for (let i = 0; i < Math.round(ms * rate / 1000 / 128); i++)
        processor.process([[]], [[new Float32Array(128)]]);
    },
  };
}

test("Input transcript preview is bounded and never repairs or changes provider text", async () => {
  const h = harness();
  const recording = recordingModule.parseRecording(recordingWave(1920), "hello.wav");
  await h.client.start("cid", { kind: "file", recording });
  h.socket().emit("portal.session.ready");
  for (const text of ["o, how are you today?", "x".repeat(1000)]) {
    h.socket().emit("portal.transcript.done", { item_id: "input", text });
    const diagnostic = h.diagnostics.at(-1);
    assert.equal(diagnostic.event, "portal.transcript.done");
    assert.equal(diagnostic.transcript_chars, text.length);
    assert.equal(diagnostic.transcript_preview, text.slice(0, 160));
    assert.equal(h.portalEvents.at(-1).payload.text, text);
  }
  await h.client.stop();
});

for (const rate of [44100, 48000]) test(`File preserves raw PCM, tail and EOF silence at ${rate} Hz without replay`, async () => {
  const h = harness(rate);
  const recording = recordingModule.parseRecording(
    recordingWave(1923, { sample: (i) => i - 1000 }),
    "input.wav",
  );
  await h.client.start("cid", { kind: "file", recording });
  assert.equal(h.options().processorOptions.capture, false);
  assert.equal(h.messages.length, 0);
  h.socket().emit("portal.session.ready");
  h.tick();
  h.tick();
  const frames = h.messages.map((event) =>
    Buffer.from(event.payload.audio, "base64"),
  );
  assert.equal(frames.length, 3);
  assert.deepEqual(
    [...frames[0], ...frames[1].subarray(0, 6)],
    [...recording.pcm],
  );
  assert.ok(frames[1].subarray(6).every((n) => n === 0));
  assert.ok(frames[2].every((n) => n === 0));
  assert.deepEqual(
    h.messages.map((event) => event.seq),
    [0, 1, 2],
  );
  assert.equal(h.progress.at(-1).sentSamples, 1923);
  h.socket().emit("portal.session.ready");
  assert.equal(h.messages.length, 3, "duplicate ready must not replay the WAV");
  await h.client.stop();
  const stoppedCount = h.messages.length;
  h.tick();
  assert.equal(h.messages.length, stoppedCount);
  assert.deepEqual(h.errors, []);
});

test("A stalled file clock fails explicitly instead of sending a catch-up burst", async () => {
  const h = harness();
  await h.client.start("cid", {
    kind: "file",
    recording: recordingModule.parseRecording(recordingWave(9600)),
  });
  h.socket().emit("portal.session.ready");
  h.tick(600);
  assert.equal(h.messages.length, 1);
  assert.match(h.errors[0], /stalled/);
  assert.equal(h.progress.at(-1).stage, "incomplete");
});

test("A second WAV uses the same session and sequence without replacing an unfinished file", async () => {
  const h = harness();
  const first = recordingModule.parseRecording(recordingWave(1923, { sample: i => i - 1000 }), "first.wav");
  const second = recordingModule.parseRecording(recordingWave(1921, { sample: () => -1234 }), "second.wav");
  await h.client.start("cid", { kind: "file", recording: first });
  h.socket().emit("portal.session.ready");
  assert.throws(() => h.client.sendRecording(second), /finish sending/);
  h.client.stopPlayback(); // Output reset must not restart or stop raw file input.
  h.tick();
  const before = h.messages.filter(m => m.type === "portal.audio.append").length;
  h.client.sendRecording(second);
  h.frame({ type: "file.frame", epoch: 1, id: 1, pcm: first.pcm.buffer, sentSamples: first.sampleCount });
  h.tick();
  h.tick();
  const sent = h.messages.filter(m => m.type === "portal.audio.append");
  assert.deepEqual(sent.map(m => m.seq), sent.map((_, i) => i));
  assert.ok(sent.every(m => m.epoch === 1));
  const frames = sent.map(m => Buffer.from(m.payload.audio, "base64"));
  assert.deepEqual([...frames[0], ...frames[1].subarray(0, 6)], [...first.pcm]);
  assert.deepEqual([...frames[before], ...frames[before + 1].subarray(0, 2)], [...second.pcm]);
  assert.ok(frames[before + 2].every(b => b === 0));
  assert.equal(h.apiCalls.length, 1, "sending the next file must not recreate/delete a session");
  assert.equal(h.progress.at(-1).name, "second.wav");
  assert.equal(h.progress.at(-1).sentSamples, second.sampleCount);
  assert.deepEqual(h.errors, []);
  await h.client.stop();
});

test("Hiding and restoring the page keeps the file session and audio clock alive", async () => {
  const h = harness();
  await h.client.start("cid", { kind: "file", recording: recordingModule.parseRecording(recordingWave(9600)) });
  h.socket().emit("portal.session.ready");
  h.visibility(true);
  h.tick();
  h.tick();
  assert.equal(h.socket().readyState, 1);
  assert.equal(h.apiCalls.length, 1);
  assert.equal(h.messages.length, 3);
  h.visibility(false);
  h.tick();
  assert.equal(h.messages.length, 4);
  assert.deepEqual(h.errors, []);
  await h.client.stop();
});

for (const state of ["suspended", "interrupted"]) test(`Returning resumes a ${state} audio context without recreating the call`, async () => {
  const h = harness();
  await h.client.start("cid", { kind: "file", recording: recordingModule.parseRecording(recordingWave(2400)) });
  h.socket().emit("portal.session.ready");
  h.context().state = state;
  h.visibility(false);
  assert.equal(h.context().state, "running");
  assert.equal(h.context().resumes, 2); // initial activation and foreground recovery
  assert.equal(h.socket().readyState, 1);
  assert.equal(h.apiCalls.length, 1);
  assert.deepEqual(h.errors, []);
  await h.client.stop();
});
