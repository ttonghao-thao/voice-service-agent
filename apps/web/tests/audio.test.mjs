import test from "node:test";
import assert from "node:assert/strict";
import { PlaybackRing, Resampler, pcm16 } from "../public/dsp.js";
const rms = (a) => Math.sqrt(a.reduce((s, x) => s + x * x, 0) / a.length);
function tone(hz, n, rate = 48000) {
  return Float32Array.from({ length: n }, (_, i) =>
    Math.sin((2 * Math.PI * hz * i) / rate),
  );
}
test("48k to 24k streaming keeps continuous duration across irregular blocks", () => {
  const r = new Resampler(48000, 24000);
  let n = 0;
  for (let i = 0; i < 48000; i += 128)
    n += r.push(new Float32Array(Math.min(128, 48000 - i))).length;
  assert.ok(n >= 23980 && n <= 24000);
});
test("anti-aliasing attenuates above-Nyquist signal", () => {
  const low = new Resampler(48000, 24000).push(tone(1000, 48000)).slice(100);
  const high = new Resampler(48000, 24000).push(tone(18000, 48000)).slice(100);
  assert.ok(rms(low) > 0.65);
  assert.ok(rms(high) / rms(low) < 0.01);
});
test("44.1k resampling has stable duration", () => {
  assert.ok(
    Math.abs(
      new Resampler(44100, 24000).push(new Float32Array(44100)).length - 24000,
    ) < 30,
  );
});
test("ring overflow fails and clear never resumes old samples", () => {
  const ring = new PlaybackRing(24000);
  ring.push(new Float32Array(5000).fill(0.5), "old");
  const output = new Float32Array(128);
  ring.read(output);
  assert.ok(output[0] > 0.1);
  ring.clear();
  ring.read(output);
  assert.ok(output.every((x) => x === 0));
  assert.equal(ring.played.size, 0);
  assert.throws(
    () => ring.push(new Float32Array(24001), "overflow"),
    /AUDIO_BACKPRESSURE/,
  );
});
test("PCM16 is signed little endian and clipped", () => {
  const d = new DataView(pcm16([-2, -1, 0, 1, 2]));
  assert.equal(d.getInt16(0, true), -32768);
  assert.equal(d.getInt16(6, true), 32767);
  assert.equal(d.getInt16(4, true), 0);
});
test("short final speech drains below startup buffer", () => {
  const ring = new PlaybackRing(48000);
  ring.push(new Float32Array(100).fill(0.5), "short");
  const output = new Float32Array(128);
  ring.read(output, 1, true);
  assert.equal(ring.length, 0);
  assert.equal(output[0], 0.5);
  assert.equal(output[127], 0);
});
