import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { PlaybackRing, Resampler, pcm16 } from "../public/dsp.js";
import { loadRecordingModule, recordingWave } from "./recording-fixtures.mjs";

function audioHarness(rate = 48000, onDiagnostics = () => {}, options = {}) {
  let Processor;
  const messages = [];
  const sources = [];
  const recordingProgress = [];
  const requests = [];
  const errors = [];
  const states = [];
  let microphoneRequests = 0;
  let node;
  const source = readFileSync(
    new URL("../public/audio-worklet.js", import.meta.url),
    "utf8",
  ).replace(/^import .*;\n/, "");
  const processorGlobals = {
    Resampler,
    PlaybackRing,
    pcm16,
    sampleRate: rate,
    currentFrame: options.initialFrame || 0,
    AudioWorkletProcessor: class {
      port = {
        postMessage: (message) => {
          messages.push(message);
          if (message.type !== "file.ready" || !options.delayFileReady)
            node?.port.onmessage?.({ data: message });
        },
      };
    },
    registerProcessor: (_, implementation) => {
      Processor = implementation;
    },
  };
  vm.runInNewContext(source, processorGlobals);
  const processor = new Processor();
  const send = (data) => processor.port.onmessage({ data });
  const output = new Float32Array(128);
  const process = (input = new Float32Array(128)) => {
    processor.process([[input]], [[output]]);
    processorGlobals.currentFrame += output.length;
    return output;
  };
  let socket;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      socket = this;
    }
    bufferedAmount = 0;
    close() {
      this.readyState = 3;
    }
    sent = [];
    send(message) {
      this.sent.push(JSON.parse(message));
    }
    emit(type, payload = {}) {
      this.onmessage({ data: JSON.stringify({ type, epoch: 1, payload }) });
    }
  }
  node = { port: { postMessage: send }, disconnect() {} };
  const exports = {};
  const clientSource = ts.transpileModule(
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
  vm.runInNewContext(clientSource, {
    exports,
    require: (path) =>
      path.includes("recordedAudio")
        ? loadRecordingModule()
        : {
            api: async (path, init = {}) => {
              requests.push({ path, method: init.method });
              if (options.api) return options.api(path, init);
              return {
                voice_session_id: "sid",
                epoch: 1,
                request_revision: 0,
                ws_url: "/stream",
              };
            },
          },
    WebSocket: Socket,
    AudioContext: class {
      sampleRate = rate;
      get currentTime() {
        return processorGlobals.currentFrame / rate;
      }
      audioWorklet = { addModule: async () => {} };
      async resume() {}
      async close() {}
      createMediaStreamSource() {
        return { connect() {} };
      }
      createBuffer(channels, length, sampleRate) {
        const data = new Float32Array(length);
        return {
          length,
          sampleRate,
          duration: length / sampleRate,
          numberOfChannels: channels,
          copyToChannel: (samples) => data.set(samples),
          getChannelData: () => data,
        };
      }
      createBufferSource() {
        const context = this;
        const source = {
          context,
          buffer: null,
          onended: null,
          starts: 0,
          stops: 0,
          connected: null,
          disconnected: false,
          ended: false,
          connect(destination) {
            this.connected = destination;
          },
          disconnect() {
            this.disconnected = true;
          },
          start(when = context.currentTime, offset = 0) {
            assert.equal(
              offset,
              0,
              "A file source must begin at its first sample",
            );
            this.starts++;
            this.startedAt = when;
          },
          stop() {
            this.stops++;
          },
        };
        sources.push(source);
        return source;
      }
    },
    AudioWorkletNode: class {
      constructor() {
        return { ...node, connect() {} };
      }
    },
    navigator: {
      mediaDevices: {
        getUserMedia: async () => {
          microphoneRequests++;
          return { getTracks: () => [], getAudioTracks: () => [] };
        },
      },
    },
    document: { addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
    URL,
    location: { href: "https://example.test/", protocol: "https:" },
    atob,
    btoa,
    performance,
    Float32Array,
    ArrayBuffer,
    crypto: { randomUUID: () => "event-id" },
    console,
  });
  const client = new exports.VoiceClient(
    () => {},
    (state) => states.push(state),
    (error) => errors.push(error),
    () => {},
    onDiagnostics,
    (progress) => recordingProgress.push(progress),
  );
  client.node = node;
  client.epoch = 1;
  client.suppressed = false;
  send({ type: "reset", epoch: 1 });
  send({ type: "ready", epoch: 1 });
  return {
    processor,
    client,
    send,
    process,
    messages,
    sources,
    requests,
    errors,
    states,
    recordingProgress,
    get microphoneRequests() {
      return microphoneRequests;
    },
    recording(samples = 2500, options) {
      return loadRecordingModule().parseRecording(
        recordingWave(samples, options),
        "reference.wav",
      );
    },
    deliverWorklet(message) {
      node?.port.onmessage?.({ data: message });
    },
    renderFile() {
      const source = sources.findLast(
        (source) => source.starts && !source.stops,
      );
      const input = new Float32Array(128);
      if (source) {
        const data = source.buffer.getChannelData(0);
        for (let i = 0; i < input.length; i++) {
          const index = Math.floor(
            ((processorGlobals.currentFrame + i) / rate - source.startedAt) *
              source.buffer.sampleRate,
          );
          if (index >= 0 && index < data.length) input[i] = data[index];
        }
      }
      process(input);
      if (
        source &&
        !source.ended &&
        processorGlobals.currentFrame / rate >=
          source.startedAt + source.buffer.duration
      ) {
        source.ended = true;
        source.onended?.();
      }
      return output;
    },
    capture(epoch = 1) {
      client.node.port.onmessage({
        data: { type: "capture", epoch, pcm: new ArrayBuffer(3840) },
      });
    },
    async connect(ready = true, input, cid = "cid") {
      await client.start(cid, input);
      assert.ok(socket, "VoiceClient must open its WebSocket");
      if (ready) socket.emit("portal.session.ready");
      return socket;
    },
  };
}

test("Capture diagnostics distinguish pre-ready, stale and actually sent frames without content", async () => {
  const diagnostics = [];
  const { client, connect, capture } = audioHarness(48000, (d) =>
    diagnostics.push(d),
  );
  const socket = await connect(false);
  capture();
  assert.equal(socket.sent.length, 0);
  socket.emit("portal.session.ready");
  capture(0);
  capture();
  capture();
  const summary = client.captureDiagnostics();
  assert.equal(summary.captured_frames, 4);
  assert.equal(summary.pre_ready_frames, 1);
  assert.equal(summary.stale_frames, 1);
  assert.equal(summary.sent_frames, 2);
  assert.equal(summary.sent_samples_24k, 3840);
  assert.equal(summary.first_seq, 0);
  assert.equal(summary.last_seq, 1);
  assert.ok(summary.ready_at_ms >= 0);
  assert.equal(socket.sent[0].seq, 0);
  await client.stop();
  await client.stop();
  assert.equal(diagnostics.length, 2);
  assert.equal(diagnostics[1].stage, "stopped");
  assert.equal(diagnostics[1].conversation_id, "cid");
  assert.equal(diagnostics[1].voice_session_id, "sid");
  assert.ok(!("audio" in summary) && !("call_token" in summary));
});

test("Failing optional diagnostics cannot prevent voice cleanup", async () => {
  const { client, connect } = audioHarness(48000, () => {
    throw new Error("telemetry failed");
  });
  await connect();
  await client.stop();
  assert.equal(client.ws, undefined);
  assert.equal(client.node, undefined);
});

test("Stop playback clears queued audio and the next authorized reply is audible", () => {
  const { client, send, process } = audioHarness();
  send({
    type: "audio",
    epoch: 1,
    response: "old",
    samples: new Float32Array(6000).fill(0.5),
  });
  assert.ok(process().some((x) => x > 0.1));
  client.stopPlayback();
  assert.ok(process().every((x) => x === 0));
  send({
    type: "audio",
    epoch: 1,
    response: "next",
    samples: new Float32Array(6000).fill(-0.5),
  });
  assert.ok(process().some((x) => x < -0.1));
});

test("Session invalidation stays silent even when late audio arrives", () => {
  const { client, send, process } = audioHarness();
  client.clear();
  send({
    type: "audio",
    epoch: 1,
    response: "late",
    samples: new Float32Array(6000).fill(0.5),
  });
  assert.ok(process().every((x) => x === 0));
  send({ type: "ready", epoch: 2 });
  assert.ok(process().every((x) => x === 0));
});

for (const rate of [44100, 48000]) {
  test(`${rate} Hz microphone produces 24 kHz PCM with preserved tone frequency`, () => {
    const { process, messages } = audioHarness(rate);
    for (let start = 0; start < rate; start += 128) {
      process(
        Float32Array.from(
          { length: 128 },
          (_, i) => 0.5 * Math.sin((2 * Math.PI * 1000 * (start + i)) / rate),
        ),
      );
    }
    const frames = messages.filter((m) => m.type === "capture");
    assert.equal(frames.length, 12);
    for (const frame of frames) assert.equal(frame.pcm.byteLength, 3840);
    const data = new DataView(frames[5].pcm);
    let crossings = 0;
    let peak = 0;
    for (let i = 1; i < 1920; i++) {
      const current = data.getInt16(i * 2, true);
      peak = Math.max(peak, Math.abs(current));
      if (data.getInt16((i - 1) * 2, true) <= 0 && current > 0) crossings++;
    }
    assert.ok(Math.abs(crossings - 80) <= 1);
    assert.ok(peak > 15000 && peak < 17000);
  });
}

test("Late stopped-response packets are dropped while the next response still plays", async () => {
  const { client, connect, process } = audioHarness();
  const socket = await connect();
  const audio = (response, value) =>
    socket.emit("portal.audio.delta", {
      response_id: response,
      audio: Buffer.from(pcm16(new Float32Array(6000).fill(value))).toString(
        "base64",
      ),
    });
  audio("old", 0.5);
  assert.ok(process().some((x) => x > 0.1));
  client.stopPlayback();
  audio("old", 0.5);
  assert.ok(process().every((x) => x === 0));
  // The gateway suppresses the rest of the old response, including its done.
  // The next legal reply must not depend on receiving that done event.
  socket.emit("portal.playback.clear", { response_id: "old" });
  audio("next", -0.5);
  assert.ok(process().some((x) => x < -0.1));
});

test("Audio done does not lose the identity of speech still buffered for playback", async () => {
  const { client, connect, process } = audioHarness();
  const socket = await connect();
  socket.emit("portal.audio.delta", {
    response_id: "buffered",
    audio: Buffer.from(pcm16(new Float32Array(6000).fill(0.5))).toString(
      "base64",
    ),
  });
  socket.emit("portal.audio.done", { response_id: "buffered" });
  assert.ok(process().some((x) => x > 0.1));
  let stopped;
  socket.send = (event) => {
    stopped = JSON.parse(event);
  };
  client.stopPlayback();
  assert.equal(stopped.payload.response_id, "buffered");
  assert.ok(process().every((x) => x === 0));
});

test("File input waits for portal ready, starts once at sample zero and never requests a microphone", async () => {
  const harness = audioHarness();
  harness.client.setMuted(true);
  const socket = await harness.connect(false, {
    kind: "file",
    recording: harness.recording(),
  });
  for (let i = 0; i < 30; i++) harness.renderFile();
  assert.equal(harness.microphoneRequests, 0);
  assert.equal(
    harness.sources.reduce((sum, source) => sum + source.starts, 0),
    0,
  );
  assert.equal(socket.sent.length, 0);
  assert.equal(harness.recordingProgress.at(-1).stage, "waiting_ready");
  socket.emit("portal.session.ready");
  socket.emit("portal.session.ready");
  assert.equal(harness.sources.length, 1);
  assert.equal(harness.sources[0].starts, 1);
  assert.equal(harness.sources[0].connected, harness.client.node);
  assert.equal(harness.client.captureDiagnostics().muted, false);
  await harness.client.stop();
});

for (const rate of [24000, 44100, 48000]) {
  test(`${rate} Hz file input flushes the partial final frame before reporting complete and keeps sending silence`, async () => {
    const harness = audioHarness(rate);
    const recording = harness.recording(2500);
    const socket = await harness.connect(true, { kind: "file", recording });
    for (let i = 0; i < Math.ceil(rate / 128); i++) harness.renderFile();
    const progress = harness.recordingProgress;
    assert.ok(progress.some((value) => value.stage === "sending"));
    assert.ok(
      progress.some(
        (value) =>
          value.stage === "tail_pending" &&
          value.sentSamples < value.totalSamples,
      ),
    );
    assert.equal(progress.at(-1).stage, "waiting_answer");
    assert.equal(progress.at(-1).sentSamples, 2500);
    assert.equal(progress.at(-1).unsentSamples, 0);
    assert.equal(progress.at(-1).sourceEnded, true);
    const frames = socket.sent.filter(
      (event) => event.type === "portal.audio.append",
    );
    assert.ok(
      frames.length >= 10,
      "EOF must leave the realtime silence stream running",
    );
    assert.equal(frames[0].seq, 0);
    for (let index = 0; index < frames.length; index++) {
      assert.equal(frames[index].seq, index);
      assert.equal(frames[index].payload.sample_rate, 24000);
      assert.equal(
        Buffer.from(frames[index].payload.audio, "base64").byteLength,
        3840,
      );
      assert.ok(
        !("fileSamples" in frames[index].payload),
        "File cursor is private metadata",
      );
    }
    const pcm = Buffer.concat(
      frames.map((event) => Buffer.from(event.payload.audio, "base64")),
    );
    assert.ok(
      pcm.some((value) => value !== 0),
      "The recording must reach the transmitted PCM",
    );
    assert.ok(
      Buffer.from(frames.at(-1).payload.audio, "base64").every(
        (value) => value === 0,
      ),
    );
    const summary = harness.client.captureDiagnostics();
    assert.equal(summary.input_source, "file");
    assert.equal(summary.file_input_complete, true);
    assert.equal(summary.sent_file_samples, 2500);
    assert.equal(summary.track_state, "not_applicable");
    await harness.client.stop();
    assert.equal(harness.recordingProgress.at(-1).stage, "ended");
  });
}

test("A nonzero AudioContext render origin preserves the first and last recorded samples", async () => {
  const harness = audioHarness(48000, () => {}, { initialFrame: 38400 });
  const recording = harness.recording(2500, {
    sample: (index) => (index < 128 ? 20000 : index >= 2372 ? -20000 : 0),
  });
  const socket = await harness.connect(true, { kind: "file", recording });
  while (!harness.sources[0].ended) harness.renderFile();
  assert.equal(harness.client.captureDiagnostics().file_input_complete, false);
  assert.equal(harness.recordingProgress.at(-1).stage, "tail_pending");
  for (let i = 0; i < 100; i++) harness.renderFile();
  assert.equal(harness.client.captureDiagnostics().file_input_complete, true);
  const pcm = Buffer.concat(
    socket.sent
      .filter((event) => event.type === "portal.audio.append")
      .map((event) => Buffer.from(event.payload.audio, "base64")),
  );
  let positive = 0,
    negative = 0;
  for (let offset = 0; offset < pcm.length; offset += 2) {
    positive = Math.max(positive, pcm.readInt16LE(offset));
    negative = Math.min(negative, pcm.readInt16LE(offset));
  }
  assert.ok(
    positive > 18000,
    "The opening samples must survive the complete input chain",
  );
  assert.ok(
    negative < -18000,
    "The partial final frame must include the ending samples",
  );
  await harness.client.stop();
});

test("A delayed file marker fails instead of scheduling an already missed source start", async () => {
  const harness = audioHarness(48000, () => {}, {
    delayFileReady: true,
    initialFrame: 9600,
  });
  await harness.connect(true, {
    kind: "file",
    recording: harness.recording(24000),
  });
  const marker = harness.messages.find(
    (message) => message.type === "file.ready",
  );
  assert.ok(marker);
  for (let i = 0; i < 40; i++) harness.renderFile();
  harness.deliverWorklet(marker);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.sources[0].starts, 0);
  assert.equal(harness.client.ws, undefined);
  assert.equal(harness.recordingProgress.at(-1).stage, "incomplete");
  assert.equal(harness.recordingProgress.at(-1).sentSamples, 0);
  assert.ok(
    harness.errors.some((message) => /scheduling fell behind/i.test(message)),
  );
});

test("Stopping a file call during session creation prevents its late startup response from opening a socket", async () => {
  let resolveSession;
  const harness = audioHarness(48000, () => {}, {
    api(_path, init) {
      if (init.method === "POST")
        return new Promise((resolve) => {
          resolveSession = resolve;
        });
      return {};
    },
  });
  const starting = harness.client.start("cid", {
    kind: "file",
    recording: harness.recording(24000),
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof resolveSession, "function");
  await harness.client.stop();
  resolveSession({
    voice_session_id: "late-session",
    epoch: 1,
    request_revision: 0,
    ws_url: "/stream",
  });
  await starting;
  assert.equal(harness.client.ws, undefined);
  assert.equal(harness.microphoneRequests, 0);
  assert.equal(harness.sources[0].starts, 0);
  assert.equal(harness.sources[0].disconnected, true);
  assert.equal(harness.recordingProgress.at(-1).stage, "incomplete");
  assert.ok(
    harness.requests.some(
      (request) =>
        request.method === "DELETE" && request.path.endsWith("/late-session"),
    ),
  );
});

test("Stop playback leaves the file input and its sending cursor running", async () => {
  const harness = audioHarness();
  const socket = await harness.connect(true, {
    kind: "file",
    recording: harness.recording(24000),
  });
  for (let i = 0; i < 100; i++) harness.renderFile();
  const before = harness.client.captureDiagnostics().sent_file_samples;
  const frameCount = socket.sent.filter(
    (event) => event.type === "portal.audio.append",
  ).length;
  harness.client.stopPlayback();
  for (let i = 0; i < 100; i++) harness.renderFile();
  assert.equal(harness.sources[0].stops, 0);
  assert.ok(harness.client.captureDiagnostics().sent_file_samples > before);
  assert.ok(
    socket.sent.filter((event) => event.type === "portal.audio.append").length >
      frameCount,
  );
  assert.equal(harness.client.captureDiagnostics().file_input_complete, false);
  await harness.client.stop();
});

for (const reason of ["error", "rotation", "cancel", "disconnect"]) {
  test(`File ${reason} stops an incomplete source without replaying it or opening a microphone`, async () => {
    const harness = audioHarness();
    const socket = await harness.connect(true, {
      kind: "file",
      recording: harness.recording(24000),
    });
    for (let i = 0; i < 60; i++) harness.renderFile();
    const source = harness.sources[0];
    const lateEnded = source.onended;
    if (reason === "error")
      socket.emit("portal.error", {
        code: "VOICE_TOOL_REQUIRED",
        message: "Rejected",
      });
    else if (reason === "rotation")
      socket.emit("portal.error", { code: "VOICE_SESSION_ROTATION_REQUIRED" });
    else if (reason === "disconnect") socket.onclose();
    else await harness.client.stop();
    await new Promise((resolve) => setImmediate(resolve));
    const frameCount = socket.sent.filter(
      (event) => event.type === "portal.audio.append",
    ).length;
    const reports = harness.recordingProgress.length;
    lateEnded?.();
    socket.emit("portal.session.ready");
    for (let i = 0; i < 60; i++) harness.renderFile();
    assert.equal(harness.microphoneRequests, 0);
    assert.equal(harness.sources.length, 1);
    assert.equal(source.starts, 1);
    assert.equal(source.stops, 1);
    assert.equal(source.disconnected, true);
    assert.equal(harness.client.ws, undefined);
    assert.equal(harness.recordingProgress.at(-1).stage, "incomplete");
    assert.ok(harness.recordingProgress.at(-1).unsentSamples > 0);
    assert.equal(harness.recordingProgress.length, reports);
    assert.equal(
      socket.sent.filter((event) => event.type === "portal.audio.append")
        .length,
      frameCount,
    );
    assert.equal(
      harness.requests.filter((request) => request.method === "POST").length,
      1,
    );
  });
}
