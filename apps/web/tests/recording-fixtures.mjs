import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

export function loadRecordingModule() {
  const exports = {};
  const source = ts.transpileModule(
    readFileSync(
      new URL("../src/audio/recordedAudio.ts", import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  vm.runInNewContext(source, { exports, ArrayBuffer, DataView, Float32Array });
  return exports;
}

export function waveFormat(overrides = {}) {
  const values = {
    format: 1,
    channels: 1,
    rate: 24000,
    byteRate: 48000,
    blockAlign: 2,
    bits: 16,
    ...overrides,
  };
  const bytes = new Uint8Array(16);
  const view = new DataView(bytes.buffer);
  view.setUint16(0, values.format, true);
  view.setUint16(2, values.channels, true);
  view.setUint32(4, values.rate, true);
  view.setUint32(8, values.byteRate, true);
  view.setUint16(12, values.blockAlign, true);
  view.setUint16(14, values.bits, true);
  return bytes;
}

export function riffWave(chunks) {
  const length =
    12 +
    chunks.reduce(
      (n, chunk) => n + 8 + chunk.bytes.length + (chunk.bytes.length % 2),
      0,
    );
  const bytes = new Uint8Array(length);
  const view = new DataView(bytes.buffer);
  const putText = (offset, text) => {
    for (let i = 0; i < text.length; i++)
      bytes[offset + i] = text.charCodeAt(i);
  };
  putText(0, "RIFF");
  view.setUint32(4, length - 8, true);
  putText(8, "WAVE");
  let cursor = 12;
  for (const chunk of chunks) {
    putText(cursor, chunk.id);
    view.setUint32(cursor + 4, chunk.bytes.length, true);
    bytes.set(chunk.bytes, cursor + 8);
    cursor += 8 + chunk.bytes.length + (chunk.bytes.length % 2);
  }
  return bytes.buffer;
}

export function recordingWave(sampleCount = 2400, options = {}) {
  const pcm = new Uint8Array(sampleCount * 2);
  const data = new DataView(pcm.buffer);
  for (let i = 0; i < sampleCount; i++)
    data.setInt16(
      i * 2,
      options.sample?.(i) ??
        Math.round(12000 * Math.sin((2 * Math.PI * 1000 * i) / 24000)),
      true,
    );
  return riffWave([
    { id: "fmt ", bytes: waveFormat(options.format) },
    ...(options.beforeData || []),
    { id: "data", bytes: pcm },
    ...(options.afterData || []),
  ]);
}
