import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { ConnectionError, RoomEvent, Track } from "livekit-client";
import ts from "typescript";

const source = ts.transpileModule(
  readFileSync(new URL("../src/audio/LiveKitMedia.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText;

function roomHarness() {
  const room = new EventEmitter();
  room.connect = async () => { room.emit(RoomEvent.SignalConnected); };
  room.startAudio = async () => {};
  room.localParticipant = { publishTrack: async () => {} };
  room.disconnect = async () => { room.emit(RoomEvent.Disconnected); };
  const exports = {};
  vm.runInNewContext(source, {
    exports,
    require: () => ({ Room: class { constructor() { return room; } }, RoomEvent, Track, ConnectionError }),
  });
  const media = new exports.LiveKitMedia();
  let closed = 0;
  return {
    room,
    start: () => media.connect({ url: "wss://voice.test", token: "secret", agent_identity: "agent" }, {}, {},
      () => { closed++; }),
    close: () => media.close(),
    closed: () => closed,
  };
}

for (const [stage, code] of [
  ["signal", "LIVEKIT_SIGNAL_FAILED"],
  ["rejected", "LIVEKIT_JOIN_REJECTED"],
  ["media", "LIVEKIT_MEDIA_FAILED"],
  ["playback", "LIVEKIT_PLAYBACK_FAILED"],
  ["publish", "LIVEKIT_PUBLISH_FAILED"],
]) test(`LiveKit reports ${stage} startup failure without triggering call teardown twice`, async () => {
  const h = roomHarness();
  const fail = async () => {
    h.room.emit(RoomEvent.Disconnected);
    if (stage === "rejected") throw ConnectionError.notAllowed("private-connection-detail", 403);
    throw new Error("private-connection-detail");
  };
  if (stage === "media") {
    h.room.connect = async () => {
      h.room.emit(RoomEvent.SignalConnected);
      await fail();
    };
  } else if (stage === "playback") {
    h.room.startAudio = fail;
  } else if (stage === "publish") {
    h.room.localParticipant.publishTrack = fail;
  } else {
    h.room.connect = fail;
  }
  await assert.rejects(h.start, (error) => {
    assert.ok(error.message.includes(code));
    assert.ok(!error.message.includes("private-connection-detail"));
    return true;
  });
  await h.close();
  assert.equal(h.closed(), 0);
});

test("LiveKit treats a room lost while publishing as a startup failure", async () => {
  const h = roomHarness();
  h.room.localParticipant.publishTrack = async () => { h.room.emit(RoomEvent.Disconnected); };
  await assert.rejects(h.start, /LIVEKIT_PUBLISH_FAILED/);
  assert.equal(h.closed(), 0);
});

test("LiveKit still reports a room lost after startup and ignores intentional cleanup", async () => {
  const h = roomHarness();
  await h.start();
  h.room.emit(RoomEvent.Disconnected);
  assert.equal(h.closed(), 1);
  await h.close();
  assert.equal(h.closed(), 1);
});
