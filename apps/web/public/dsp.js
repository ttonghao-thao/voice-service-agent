// Streaming, 64-tap Blackman-windowed sinc resampling. No audio files or per-frame AudioBufferSources.
export class Resampler {
  constructor(inputRate, outputRate) {
    this.ratio = inputRate / outputRate;
    this.buffer = new Float32Array(65536);
    this.total = 0;
    this.position = 0;
    this.half = 32;
    this.phases = 256;
    const cutoff = 0.45 * Math.min(1, outputRate / inputRate);
    this.table = Array.from({ length: this.phases }, (_, phase) => {
      const coefficients = new Float64Array(64);
      let sum = 0;
      for (let j = 0; j < 64; j++) {
        const x = j - 31 - phase / this.phases;
        const sinc =
          Math.abs(x) < 1e-8
            ? 2 * cutoff
            : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
        const window =
          0.42 -
          0.5 * Math.cos((2 * Math.PI * j) / 63) +
          0.08 * Math.cos((4 * Math.PI * j) / 63);
        coefficients[j] = sinc * window;
        sum += coefficients[j];
      }
      for (let j = 0; j < 64; j++) coefficients[j] /= sum;
      return coefficients;
    });
  }
  push(input) {
    const output = [];
    for (const value of input) {
      this.buffer[this.total++ % this.buffer.length] = value;
      while (this.position + this.half < this.total) {
        const center = Math.floor(this.position);
        const phase = Math.min(
          255,
          Math.floor((this.position - center) * this.phases),
        );
        const coefficients = this.table[phase];
        let sample = 0;
        for (let j = 0; j < 64; j++) {
          const index = center + j - 31;
          if (index >= 0)
            sample += this.buffer[index % this.buffer.length] * coefficients[j];
        }
        output.push(sample);
        this.position += this.ratio;
      }
    }
    return Float32Array.from(output);
  }
}
export class PlaybackRing {
  constructor(rate) {
    this.rate = rate;
    this.items = [];
    this.length = 0;
    this.target = Math.round(rate * 0.16);
    this.started = false;
    this.played = new Map();
  }
  clear() {
    this.items = [];
    this.length = 0;
    this.started = false;
    this.played.clear();
  }
  push(samples, response) {
    if (this.length + samples.length > this.rate)
      throw new Error("AUDIO_BACKPRESSURE");
    this.items.push({ samples, response, offset: 0 });
    this.length += samples.length;
  }
  read(output, gain = 1, drain = false) {
    output.fill(0);
    if (!this.started && (this.length >= this.target || drain))
      this.started = true;
    if (!this.started) return;
    for (let i = 0; i < output.length; i++) {
      const item = this.items[0];
      if (!item) {
        this.started = false;
        break;
      }
      output[i] = item.samples[item.offset++] * gain;
      this.length--;
      this.played.set(item.response, (this.played.get(item.response) || 0) + 1);
      if (item.offset === item.samples.length) this.items.shift();
    }
  }
}
export function pcm16(input) {
  const out = new ArrayBuffer(input.length * 2);
  const view = new DataView(out);
  for (let i = 0; i < input.length; i++) {
    const x = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(i * 2, Math.round(x * (x < 0 ? 32768 : 32767)), true);
  }
  return out;
}
