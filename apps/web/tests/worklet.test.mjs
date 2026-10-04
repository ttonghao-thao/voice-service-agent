import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { PlaybackRing, Resampler, pcm16 } from "../public/dsp.js";

function audioHarness(rate = 48000) {
  let Processor;
  const messages = [];
  const source = readFileSync(
    new URL("../public/audio-worklet.js", import.meta.url),
    "utf8",
  ).replace(/^import .*;\n/, "");
  vm.runInNewContext(source, {
    Resampler,
    PlaybackRing,
    pcm16,
    sampleRate: rate,
    AudioWorkletProcessor: class {
      port = { postMessage: (message) => messages.push(message) };
    },
    registerProcessor: (_, implementation) => {
      Processor = implementation;
    },
  });
  const processor = new Processor();
  const send = (data) => processor.port.onmessage({ data });
  const output = new Float32Array(128);
  const process = (input = new Float32Array(128)) => {
    processor.process([[input]], [[output]]);
    return output;
  };
  let socket;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      socket = this;
    }
    close() {}
    send() {}
    emit(type, payload = {}) {
      this.onmessage({ data: JSON.stringify({ type, epoch: 1, payload }) });
    }
  }
  const node = { port: { postMessage: send }, disconnect() {} };
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
    require: () => ({
      api: async () => ({
        voice_session_id: "sid",
        epoch: 1,
        request_revision: 0,
        ws_url: "/stream",
      }),
    }),
    WebSocket: Socket,
    AudioContext: class {
      audioWorklet = { addModule: async () => {} };
      async resume() {}
      async close() {}
      createMediaStreamSource() {
        return { connect() {} };
      }
    },
    AudioWorkletNode: class {
      constructor() {
        return { ...node, connect() {} };
      }
    },
    navigator: {
      mediaDevices: {
        getUserMedia: async () => ({
          getTracks: () => [],
          getAudioTracks: () => [],
        }),
      },
    },
    document: { addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
    URL,
    location: { href: "https://example.test/", protocol: "https:" },
    atob,
  });
  const client = new exports.VoiceClient(
    () => {},
    () => {},
    () => {},
    () => {},
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
    async connect() {
      await client.start("cid");
      assert.ok(socket, "VoiceClient must open its WebSocket");
      socket.emit("portal.session.ready");
      return socket;
    },
  };
}

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

test("A drained buffer does not finish playback before provider audio done", () => {
  const { send, process, messages } = audioHarness();
  send({ type: "audio", epoch: 1, response: "open", samples: new Float32Array(6000).fill(0.2) });
  for (let i = 0; i < 240; i++) process();
  const before = messages.filter((m) => m.type === "ack" && m.response === "open");
  assert.ok(before.length && before.every((m) => m.done === false));
  send({ type: "done", epoch: 1, response: "open" });
  for (let i = 0; i < 240; i++) process();
  const done = messages.filter((m) => m.type === "ack" && m.response === "open" && m.done);
  assert.equal(done.length, 1);
  assert.equal(done[0].samples, 6000);
});

test("An ACK ending cannot mark an overlapping unfinished answer complete", () => {
  const { send, process, messages } = audioHarness();
  send({ type: "audio", epoch: 1, response: "ack", samples: new Float32Array(1920).fill(0.2) });
  send({ type: "done", epoch: 1, response: "ack" });
  send({ type: "audio", epoch: 1, response: "answer", samples: new Float32Array(1920).fill(0.3) });
  for (let i = 0; i < 240; i++) process();
  const acks = messages.filter((m) => m.type === "ack");
  assert.equal(acks.find((m) => m.response === "ack")?.done, true);
  assert.equal(acks.find((m) => m.response === "answer")?.done, false);
  send({ type: "done", epoch: 1, response: "answer" });
  for (let i = 0; i < 240; i++) process();
  const done = messages.filter((m) => m.type === "ack" && m.response === "answer" && m.done);
  assert.equal(done.length, 1);
  assert.equal(done[0].samples, 1920);
});
