import { Resampler, PlaybackRing, pcm16 } from "./dsp.js";
class PortalAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capture = new Resampler(sampleRate, 24000);
    this.playback = new Resampler(24000, sampleRate);
    this.ring = new PlaybackRing(sampleRate);
    this.chunk = new Float32Array(1920);
    this.used = 0;
    this.epoch = -1;
    this.suppressed = true;
    this.muted = false;
    this.volume = 0.8;
    this.ticks = 0;
    this.finished = false;
    this.response = null;
    this.sourceSamples = new Map();
    this.resamplers = new Map();
    this.ended = new Set();
    this.port.onmessage = ({ data: d }) => {
      if (d.type === "reset") {
        this.epoch = d.epoch;
        this.suppressed = d.suppressed !== false;
        this.ring.clear();
        this.playback = new Resampler(24000, sampleRate);
        this.sourceSamples.clear();
        this.resamplers.clear();
        this.ended.clear();
        this.response = null;
        this.finished = false;
      }
      if (d.type === "ready" && d.epoch === this.epoch) this.suppressed = false;
      if (d.type === "mute") this.muted = d.value;
      if (d.type === "volume") this.volume = d.value;
      if (d.type === "audio" && !this.suppressed && d.epoch === this.epoch) {
        if (this.ended.has(d.response)) return;
        if (!this.resamplers.has(d.response))
          this.resamplers.set(d.response, new Resampler(24000, sampleRate));
        this.response = d.response;
        this.sourceSamples.set(
          d.response,
          (this.sourceSamples.get(d.response) || 0) + d.samples.length,
        );
        this.finished = false;
        try {
          this.ring.push(this.resamplers.get(d.response).push(d.samples), d.response);
        } catch (e) {
          this.suppressed = true;
          this.ring.clear();
          this.port.postMessage({ type: "error", code: "AUDIO_BACKPRESSURE" });
        }
      }
      if (d.type === "done" && !this.suppressed && d.epoch === this.epoch) {
        if (this.ended.has(d.response)) return;
        this.ended.add(d.response);
        this.finished = true;
        if (this.resamplers.has(d.response)) {
          try {
            this.ring.push(
              this.resamplers.get(d.response).push(new Float32Array(32)),
              d.response,
            );
          } catch {
            this.suppressed = true;
            this.ring.clear();
            this.port.postMessage({
              type: "error",
              code: "AUDIO_BACKPRESSURE",
            });
          }
        }
      }
    };
  }
  process(inputs, outputs) {
    const output = outputs[0][0];
    if (!output) return true;
    const source = inputs[0]?.[0] || new Float32Array(output.length);
    const samples = this.capture.push(
      this.muted ? new Float32Array(source.length) : source,
    );
    for (const value of samples) {
      this.chunk[this.used++] = value;
      if (this.used === 1920) {
        const pcm = pcm16(this.chunk);
        this.port.postMessage({ type: "capture", pcm, epoch: this.epoch }, [
          pcm,
        ]);
        this.used = 0;
      }
    }
    if (this.suppressed) output.fill(0);
    else this.ring.read(output, this.volume, this.ended.has(this.ring.items[0]?.response));
    this.ticks += output.length;
    if (this.ticks >= sampleRate / 2) {
      this.ticks = 0;
      for (const [response, played] of this.ring.played) {
        const done = this.ended.has(response) &&
          !this.ring.items.some((item) => item.response === response);
        this.port.postMessage({
          type: "ack",
          response,
          epoch: this.epoch,
          samples: done ? this.sourceSamples.get(response) || 0 : Math.min(
            this.sourceSamples.get(response) || 0,
            Math.floor((played * 24000) / sampleRate),
          ),
          buffered: this.ring.length,
          done,
        });
        if (done) {
          this.ring.played.delete(response);
          this.sourceSamples.delete(response);
          this.resamplers.delete(response);
          this.ended.delete(response);
        }
      }
    }
    return true;
  }
}
registerProcessor("portal-audio", PortalAudioProcessor);
