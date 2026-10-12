import test from "node:test";
import assert from "node:assert/strict";
import {
  loadRecordingModule,
  recordingWave,
  riffWave,
  waveFormat,
} from "./recording-fixtures.mjs";

const { parseRecording, prepareRecording } = loadRecordingModule();

test("PCM WAV validation preserves source samples and tolerates padded metadata chunks", () => {
  const bytes = recordingWave(4, {
    sample: (index) => [-32768, -1, 0, 32767][index],
    beforeData: [{ id: "JUNK", bytes: new Uint8Array([1, 2, 3]) }],
    afterData: [{ id: "LIST", bytes: new Uint8Array([4, 5]) }],
  });
  const recording = parseRecording(bytes, "reference.wav");
  assert.equal(recording.name, "reference.wav");
  assert.equal(recording.byteLength, bytes.byteLength);
  assert.equal(recording.sampleRate, 24000);
  assert.equal(recording.channels, 1);
  assert.equal(recording.bitsPerSample, 16);
  assert.equal(recording.sampleCount, 4);
  assert.equal(recording.durationSeconds, 4 / 24000);
  assert.equal(recording.samples[0], -1);
  assert.ok(recording.samples[1] < 0 && recording.samples[1] > -0.001);
  assert.equal(recording.samples[2], 0);
  assert.ok(recording.samples[3] > 0.999 && recording.samples[3] <= 1);
});

test("Thirty seconds is accepted while one additional sample is rejected", () => {
  assert.equal(parseRecording(recordingWave(720000)).sampleCount, 720000);
  assert.throws(
    () => parseRecording(recordingWave(720001)),
    /30|duration|long/i,
  );
});

test("Two MiB size is enforced before reading a selected file", async () => {
  let reads = 0;
  const file = {
    name: "oversized.wav",
    size: 2 * 1024 * 1024 + 1,
    async arrayBuffer() {
      reads++;
      return recordingWave();
    },
  };
  await assert.rejects(() => prepareRecording(file), /2|size|large|MiB/i);
  assert.equal(reads, 0);
  const atLimit = recordingWave(2, {
    afterData: [{ id: "JUNK", bytes: new Uint8Array(2 * 1024 * 1024 - 56) }],
  });
  assert.equal(atLimit.byteLength, 2 * 1024 * 1024);
  assert.equal(parseRecording(atLimit).sampleCount, 2);
  const bytes = recordingWave(2400, {
    afterData: [{ id: "JUNK", bytes: new Uint8Array(2 * 1024 * 1024) }],
  });
  assert.throws(() => parseRecording(bytes), /2|size|large|MiB/i);
});

test("A selected WAV is validated by its bytes before preparing its audio source", async () => {
  const bytes = recordingWave();
  const recording = await prepareRecording({
    name: "fixed.wav",
    size: bytes.byteLength,
    arrayBuffer: async () => bytes,
  });
  assert.equal(recording.sampleCount, 2400);
  await assert.rejects(
    () =>
      prepareRecording({
        name: "looks-valid.wav",
        size: 44,
        arrayBuffer: async () => new ArrayBuffer(44),
      }),
    /RIFF|WAV|format|invalid/i,
  );
});

for (const [label, format] of [
  ["float encoding", { format: 3 }],
  ["stereo", { channels: 2 }],
  ["48 kHz", { rate: 48000 }],
  ["8-bit samples", { bits: 8 }],
  ["inconsistent block alignment", { blockAlign: 4 }],
  ["inconsistent byte rate", { byteRate: 96000 }],
]) {
  test(`WAV rejects ${label}`, () => {
    assert.throws(() => parseRecording(recordingWave(2400, { format })));
  });
}

test("RIFF validation rejects false magic, mismatched size and truncated chunks", () => {
  const corruptions = [
    (bytes) => {
      new Uint8Array(bytes)[0] = 0;
    },
    (bytes) => {
      new Uint8Array(bytes)[8] = 0;
    },
    (bytes) => {
      new DataView(bytes).setUint32(4, bytes.byteLength, true);
    },
    (bytes) => {
      new DataView(bytes).setUint32(4, bytes.byteLength - 10, true);
    },
    (bytes) => {
      new DataView(bytes).setUint32(40, bytes.byteLength, true);
    },
  ];
  for (const corrupt of corruptions) {
    const bytes = recordingWave();
    corrupt(bytes);
    assert.throws(() => parseRecording(bytes));
  }
  const truncated = recordingWave().slice(0, 42);
  new DataView(truncated).setUint32(4, truncated.byteLength - 8, true);
  assert.throws(() => parseRecording(truncated));
  const noPadding = recordingWave(2400, {
    afterData: [{ id: "JUNK", bytes: new Uint8Array([1]) }],
  }).slice(0, -1);
  new DataView(noPadding).setUint32(4, noPadding.byteLength - 8, true);
  assert.throws(() => parseRecording(noPadding));
});

test("Empty, unaligned, duplicate or missing PCM chunks are rejected", () => {
  const fmt = { id: "fmt ", bytes: waveFormat() };
  const pcm = { id: "data", bytes: new Uint8Array([1, 0]) };
  for (const chunks of [
    [fmt],
    [pcm],
    [fmt, { id: "data", bytes: new Uint8Array() }],
    [fmt, { id: "data", bytes: new Uint8Array([1, 2, 3]) }],
    [fmt, fmt, pcm],
    [fmt, pcm, pcm],
  ])
    assert.throws(() => parseRecording(riffWave(chunks)));
});
