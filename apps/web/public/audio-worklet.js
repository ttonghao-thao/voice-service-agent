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
    this.captureOriginFrame = null;
    this.file = null;
    this.port.onmessage = ({ data: d }) => {
      if (d.type === "reset") {
        this.epoch = d.epoch;
        this.suppressed = d.suppressed !== false;
        this.ring.clear();
        this.playback = new Resampler(24000, sampleRate);
        this.sourceSamples.clear();
        this.response = null;
        this.finished = false;
      }
      if (d.type === "ready" && d.epoch === this.epoch) this.suppressed = false;
      if (d.type === "file" && d.epoch === this.epoch && !this.file) {
        if (
          !Number.isSafeInteger(d.startFrame) ||
          d.startFrame <= currentFrame ||
          !Number.isSafeInteger(d.sourceFrames) ||
          d.sourceFrames <= 0 ||
          !Number.isSafeInteger(d.totalSamples) ||
          d.totalSamples <= 0
        ) {
          this.port.postMessage({
            type: "error",
            code: "AUDIO_BACKPRESSURE",
            epoch: this.epoch,
          });
          return;
        }
        this.file = {
          startFrame: d.startFrame,
          endFrame: d.startFrame + d.sourceFrames,
          sourceFrames: d.sourceFrames,
          totalSamples: d.totalSamples,
        };
        this.port.postMessage({
          type: "file.ready",
          epoch: this.epoch,
          startFrame: d.startFrame,
        });
      }
      if (d.type === "mute") this.muted = d.value;
      if (d.type === "volume") this.volume = d.value;
      if (d.type === "audio" && !this.suppressed && d.epoch === this.epoch) {
        if (this.response !== d.response) {
          this.playback = new Resampler(24000, sampleRate);
          this.response = d.response;
        }
        this.sourceSamples.set(
          d.response,
          (this.sourceSamples.get(d.response) || 0) + d.samples.length,
        );
        this.finished = false;
        try {
          this.ring.push(this.playback.push(d.samples), d.response);
        } catch (e) {
          this.suppressed = true;
          this.ring.clear();
          this.port.postMessage({ type: "error", code: "AUDIO_BACKPRESSURE" });
        }
      }
      if (d.type === "done" && !this.suppressed && d.epoch === this.epoch) {
        this.finished = true;
        if (this.response === d.response) {
          try {
            this.ring.push(
              this.playback.push(new Float32Array(32)),
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
    if (this.captureOriginFrame === null)
      this.captureOriginFrame =
        typeof currentFrame === "number" ? currentFrame : 0;
    const firstPosition = this.capture.position;
    const samples = this.capture.push(
      this.muted ? new Float32Array(source.length) : source,
    );
    let position = firstPosition;
    for (const value of samples) {
      this.chunk[this.used++] = value;
      if (this.used === 1920) {
        const pcm = pcm16(this.chunk);
        const centerFrame = this.captureOriginFrame + Math.floor(position);
        // A file sample still contributes to FIR output until its entire
        // 64-tap window has passed. Publish the cursor only with a full frame.
        const coveredFrames = this.file
          ? Math.max(
              0,
              Math.min(
                this.file.sourceFrames,
                centerFrame - this.capture.half - this.file.startFrame,
              ),
            )
          : 0;
        const fileComplete =
          !!this.file && centerFrame >= this.file.endFrame + this.capture.half;
        const fileSamples = this.file
          ? fileComplete
            ? this.file.totalSamples
            : Math.floor(
                (coveredFrames * this.file.totalSamples) /
                  this.file.sourceFrames,
              )
          : undefined;
        this.port.postMessage(
          {
            type: "capture",
            pcm,
            epoch: this.epoch,
            ...(this.file ? { fileSamples, fileComplete } : {}),
          },
          [pcm],
        );
        this.used = 0;
      }
      position += this.capture.ratio;
    }
    if (this.suppressed) output.fill(0);
    else this.ring.read(output, this.volume, this.finished);
    this.ticks += output.length;
    if (this.ticks >= sampleRate / 2) {
      this.ticks = 0;
      for (const [response, played] of this.ring.played) {
        this.port.postMessage({
          type: "ack",
          response,
          epoch: this.epoch,
          samples: Math.min(
            this.sourceSamples.get(response) || 0,
            Math.floor((played * 24000) / sampleRate),
          ),
          buffered: this.ring.length,
          done: this.finished && this.ring.length === 0,
        });
        if (this.finished && this.ring.length === 0) {
          this.ring.played.delete(response);
          this.sourceSamples.delete(response);
        }
      }
    }
    return true;
  }
}
registerProcessor("portal-audio", PortalAudioProcessor);
