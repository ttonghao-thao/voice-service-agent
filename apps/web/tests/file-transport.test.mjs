import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { loadRecordingModule, recordingWave } from "./recording-fixtures.mjs";

const recordingModule = loadRecordingModule();
function harness() {
  const exports = {},
    messages = [],
    timers = new Map(),
    errors = [],
    progress = [];
  let socket,
    clock = 1000,
    options,
    timer = 0;
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
    emit(type) {
      this.onmessage({ data: JSON.stringify({ type, epoch: 1, payload: {} }) });
    }
  }
  vm.runInNewContext(source, {
    exports,
    require: (name) =>
      name.endsWith("recordedAudio")
        ? recordingModule
        : {
            api: async () => ({
              voice_session_id: "sid",
              epoch: 1,
              request_revision: 0,
              ws_url: "/stream",
            }),
          },
    AudioContext: class {
      audioWorklet = { addModule: async () => {} };
      resume = async () => {};
      close = async () => {};
      // No createBuffer/BufferSource: files must not enter the resampler.
    },
    AudioWorkletNode: class {
      port = { postMessage() {} };
      connect() {}
      disconnect() {}
      constructor(_context, _name, config) {
        options = config;
      }
    },
    WebSocket: Socket,
    URL,
    location: { href: "https://example.test", protocol: "https:" },
    document: { addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
    performance: { now: () => clock },
    crypto: { randomUUID: () => "event" },
    btoa,
    console: { info() {} },
    setTimeout: (callback, delay) => {
      assert.equal(delay, 80);
      timers.set(++timer, callback);
      return timer;
    },
    clearTimeout: (id) => timers.delete(id),
  });
  const client = new exports.VoiceClient(
    () => {},
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
    timers,
    socket: () => socket,
    options: () => options,
    tick(ms = 80) {
      clock += ms;
      const [id, callback] = timers.entries().next().value;
      timers.delete(id);
      callback();
    },
  };
}

test("File waits for ready, sends exact padded frames then silence, and stops without replay", async () => {
  const h = harness();
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
  assert.equal(h.timers.size, 0);
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
  assert.equal(h.timers.size, 0);
  assert.match(h.errors[0], /stalled/);
  assert.equal(h.progress.at(-1).stage, "incomplete");
});
