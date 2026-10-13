import { api, PortalEvent } from "../api";
import type { PreparedRecording } from "./recordedAudio";

export type VoiceInput =
  { kind: "microphone" } | { kind: "file"; recording: PreparedRecording };
export interface RecordingProgress {
  stage:
    | "waiting_ready"
    | "sending"
    | "tail_pending"
    | "waiting_answer"
    | "incomplete"
    | "ended";
  sourceEnded: boolean;
  totalSamples: number;
  sentSamples: number;
  unsentSamples: number;
  sampleRate: 24000;
  durationSeconds: number;
  name: string;
}
export type VoiceState =
  "closed" | "connecting" | "ready" | "reconnecting" | "error";
export interface MicrophoneDiagnostics {
  label: string;
  sampleRate?: number;
  channelCount?: number;
  echoCancellation?: boolean;
  noiseSuppression?: boolean;
  autoGainControl?: boolean;
}
export class VoiceClient {
  private context?: AudioContext;
  private node?: AudioWorkletNode;
  private media?: MediaStream;
  private ws?: WebSocket;
  private cid = "";
  private sid = "";
  private epoch = -1;
  private requestRevision = 0;
  private seq = 0;
  private ready = false;
  private generation = 0;
  private lastCapture = 0;
  private suppressed = true;
  private muted = false;
  private volume = 0.8;
  private activeResponse = "";
  private stoppedResponses = new Set<string>();
  private input: VoiceInput = { kind: "microphone" };
  private fileSource?: AudioBufferSourceNode;
  private fileArmed = false;
  private fileStarted = false;
  private fileStartFrame = 0;
  private fileSourceEnded = false;
  private sentFileSamples = 0;
  private fileComplete = false;
  private recordingStopped = true;
  private recordingStage?: RecordingProgress["stage"];
  private recordingSourceEnded = false;
  private lastRecordingProgress = 0;
  constructor(
    private onEvent: (e: PortalEvent) => void,
    private onState: (state: VoiceState) => void,
    private onError: (message: string) => void,
    private onMicrophone: (diagnostics: MicrophoneDiagnostics) => void,
    private onRecording: (progress: RecordingProgress) => void = () => {},
  ) {}
  private emitRecording(stage: RecordingProgress["stage"]) {
    if (this.input.kind !== "file") return;
    const now = performance.now();
    if (
      stage === this.recordingStage &&
      this.fileSourceEnded === this.recordingSourceEnded &&
      now - this.lastRecordingProgress < 250
    )
      return;
    this.recordingStage = stage;
    this.recordingSourceEnded = this.fileSourceEnded;
    this.lastRecordingProgress = now;
    const recording = this.input.recording;
    this.onRecording({
      stage,
      sourceEnded: this.fileSourceEnded,
      totalSamples: recording.sampleCount,
      sentSamples: this.sentFileSamples,
      unsentSamples: recording.sampleCount - this.sentFileSamples,
      sampleRate: 24000,
      durationSeconds: recording.durationSeconds,
      name: recording.name,
    });
  }
  private armRecording() {
    if (
      this.input.kind !== "file" ||
      !this.context ||
      !this.node ||
      this.fileArmed
    )
      return;
    this.fileArmed = true;
    this.fileStartFrame = Math.ceil(
      (this.context.currentTime + 0.08) * this.context.sampleRate,
    );
    this.node.port.postMessage({
      type: "file",
      epoch: this.epoch,
      startFrame: this.fileStartFrame,
      sourceFrames: Math.ceil(
        (this.input.recording.sampleCount * this.context.sampleRate) / 24000,
      ),
      totalSamples: this.input.recording.sampleCount,
    });
  }
  private startScheduledRecording(startFrame: number) {
    if (
      this.input.kind !== "file" ||
      !this.context ||
      !this.fileSource ||
      this.fileStarted
    )
      return;
    const startTime = this.fileStartFrame / this.context.sampleRate;
    if (
      startFrame !== this.fileStartFrame ||
      this.context.currentTime >= startTime
    ) {
      void this.stop();
      this.onError("File input scheduling fell behind. Restart the file call.");
      return;
    }
    try {
      this.fileSource.start(startTime);
      this.fileStarted = true;
      this.emitRecording("sending");
    } catch {
      void this.stop();
      this.onError(
        "The recording could not be started. Restart the file call.",
      );
    }
  }
  private visibility = () => {
    if (document.hidden) {
      void this.stop();
      this.onError(
        "Voice stopped when the page went into the background. Resume when you return.",
      );
    }
  };
  private offline = () => {
    void this.stop();
    this.onError("The network disconnected and voice playback stopped.");
  };
  async start(cid: string, input: VoiceInput = { kind: "microphone" }) {
    await this.stop();
    const generation = ++this.generation;
    this.cid = cid;
    this.input = input;
    this.fileArmed = false;
    this.fileStarted = false;
    this.fileSourceEnded = false;
    this.fileComplete = false;
    this.sentFileSamples = 0;
    this.recordingStage = undefined;
    this.recordingSourceEnded = false;
    this.lastRecordingProgress = 0;
    this.recordingStopped = input.kind !== "file";
    if (input.kind === "file") {
      this.muted = false;
      this.emitRecording("waiting_ready");
    }
    this.onState("connecting");
    try {
      if (input.kind === "microphone" && !navigator.mediaDevices?.getUserMedia)
        throw new Error(
          "The browser cannot access the microphone. Verify HTTPS, browser support, and microphone permission.",
        );
      const context = new AudioContext({ latencyHint: "interactive" });
      this.context = context;
      await context.resume();
      if (generation !== this.generation) {
        await context.close().catch(() => {});
        return;
      }
      if (input.kind === "microphone") {
        const media = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (generation !== this.generation) {
          media.getTracks().forEach((t) => t.stop());
          return;
        }
        this.media = media;
        const microphone = media.getAudioTracks()[0];
        const microphoneSettings = microphone?.getSettings() || {};
        this.onMicrophone({
          label: microphone?.label || "Default microphone",
          sampleRate: microphoneSettings.sampleRate,
          channelCount: microphoneSettings.channelCount,
          echoCancellation: microphoneSettings.echoCancellation,
          noiseSuppression: microphoneSettings.noiseSuppression,
          autoGainControl: microphoneSettings.autoGainControl,
        });
      }
      await context.audioWorklet.addModule("/audio-worklet.js");
      if (generation !== this.generation) return;
      this.node = new AudioWorkletNode(context, "portal-audio", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      if (input.kind === "microphone") {
        context.createMediaStreamSource(this.media!).connect(this.node);
      } else {
        const recording = input.recording;
        if (
          !recording.sampleCount ||
          recording.sampleCount !== recording.samples.length ||
          recording.sampleCount > 720000 ||
          recording.sampleRate !== 24000
        )
          throw new Error(
            "Choose a validated recording no longer than 30 seconds.",
          );
        const buffer = context.createBuffer(1, recording.sampleCount, 24000);
        buffer.getChannelData(0).set(recording.samples);
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(this.node);
        this.fileSource = source;
        source.onended = () => {
          if (generation !== this.generation || this.fileSource !== source)
            return;
          this.fileSourceEnded = true;
          this.emitRecording(
            this.fileComplete ? "waiting_answer" : "tail_pending",
          );
        };
      }
      this.node.connect(context.destination);
      const session = await api<{
        voice_session_id: string;
        epoch: number;
        request_revision: number;
        ws_url: string;
      }>(`/conversations/${cid}/voice-sessions`, {
        method: "POST",
        body: "{}",
      });
      if (generation !== this.generation) {
        await api(
          `/conversations/${cid}/voice-sessions/${session.voice_session_id}`,
          { method: "DELETE" },
        );
        return;
      }
      this.sid = session.voice_session_id;
      this.epoch = session.epoch;
      this.requestRevision = session.request_revision;
      this.seq = 0;
      this.suppressed = true;
      this.node.port.postMessage({ type: "reset", epoch: this.epoch });
      this.setMuted(this.muted);
      this.setVolume(this.volume);
      const url = new URL(session.ws_url, location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      this.ws = new WebSocket(url);
      this.node.port.onmessage = ({ data: d }) => {
        if (d.type === "error") {
          this.clear();
          void this.stop();
          this.onError("Audio playback fell behind. Restart voice.");
          return;
        }
        if (
          !this.ready ||
          this.ws?.readyState !== WebSocket.OPEN ||
          d.epoch !== this.epoch
        )
          return;
        if (d.type === "file.ready") {
          this.startScheduledRecording(d.startFrame);
          return;
        }
        if (d.type === "capture") {
          const clock = performance.now();
          if (this.lastCapture && clock - this.lastCapture > 500) {
            void this.stop();
            this.onError("Audio capture stalled. Restart voice.");
            return;
          }
          this.lastCapture = clock;
          if (this.ws.bufferedAmount > 32000) {
            void this.stop();
            this.onError("Network audio fell behind. Restart voice.");
            return;
          }
          const bytes = new Uint8Array(d.pcm);
          let binary = "";
          for (const b of bytes) binary += String.fromCharCode(b);
          try {
            this.ws.send(
              JSON.stringify({
                type: "portal.audio.append",
                event_id: crypto.randomUUID(),
                epoch: this.epoch,
                seq: this.seq,
                payload: {
                  format: "pcm16",
                  sample_rate: 24000,
                  audio: btoa(binary),
                },
              }),
            );
          } catch {
            void this.stop();
            this.onError("Voice audio could not be sent. Restart the call.");
            return;
          }
          this.seq++;
          if (this.input.kind === "file" && this.fileStarted) {
            const total = this.input.recording.sampleCount;
            if (
              Number.isInteger(d.fileSamples) &&
              d.fileSamples >= 0 &&
              d.fileSamples <= total
            )
              this.sentFileSamples = Math.max(
                this.sentFileSamples,
                d.fileSamples,
              );
            if (d.fileComplete === true && this.sentFileSamples === total)
              this.fileComplete = true;
            if (!this.recordingStopped)
              this.emitRecording(
                this.fileComplete
                  ? "waiting_answer"
                  : this.fileSourceEnded
                    ? "tail_pending"
                    : "sending",
              );
          }
        } else if (d.type === "ack" && !this.suppressed) {
          if (d.done && this.activeResponse === d.response)
            this.activeResponse = "";
          this.ws.send(
            JSON.stringify({
              type: "portal.playback.ack",
              epoch: this.epoch,
              payload: {
                response_id: d.response,
                played_samples: d.samples,
                finished: d.done === true,
              },
            }),
          );
        }
      };
      this.ws.onmessage = ({ data }) => {
        if (generation !== this.generation) return;
        let event: PortalEvent;
        try {
          event = JSON.parse(data);
        } catch {
          void this.stop();
          this.onError("Invalid voice event");
          return;
        }
        if (event.epoch !== this.epoch) return;
        this.requestRevision = Math.max(
          this.requestRevision,
          event.request_revision || 0,
        );
        if (event.type === "portal.session.ready") {
          this.ready = true;
          this.suppressed = false;
          this.lastCapture = 0;
          this.node?.port.postMessage({ type: "ready", epoch: this.epoch });
          this.onState("ready");
          this.armRecording();
        }
        if (event.type === "portal.audio.delta" && !this.suppressed) {
          const response = String(event.payload.response_id);
          if (this.stoppedResponses.has(response)) return;
          this.activeResponse = response;
          const binary = atob(String(event.payload.audio));
          const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
          const view = new DataView(bytes.buffer);
          const samples = new Float32Array(bytes.length / 2);
          for (let i = 0; i < samples.length; i++)
            samples[i] = view.getInt16(i * 2, true) / 32768;
          this.node?.port.postMessage(
            {
              type: "audio",
              samples,
              epoch: this.epoch,
              response: event.payload.response_id,
            },
            [samples.buffer],
          );
        }
        if (event.type === "portal.audio.done") {
          this.stoppedResponses.delete(String(event.payload.response_id));
          this.node?.port.postMessage({
            type: "done",
            epoch: this.epoch,
            response: event.payload.response_id,
          });
        }
        if (event.type === "portal.playback.clear")
          this.clearPlaybackResponse(
            event.payload.response_id
              ? String(event.payload.response_id)
              : undefined,
          );
        if (event.type === "portal.error") {
          if (event.payload.code === "VOICE_SESSION_ROTATION_REQUIRED") {
            this.onEvent(event);
            void this.rotate(generation);
            return;
          }
          this.clear();
          this.onError(String(event.payload.message));
          void this.stop();
        }
        this.onEvent(event);
      };
      this.ws.onclose = () => {
        if (generation === this.generation) {
          this.clear();
          void this.stop();
          this.onState("closed");
        }
      };
      this.ws.onerror = () => {
        if (generation !== this.generation) return;
        this.clear();
        this.onError(
          "Voice connection failed. Text questions remain available.",
        );
        void this.stop();
      };
      document.addEventListener("visibilitychange", this.visibility);
      window.addEventListener("offline", this.offline);
    } catch (error) {
      if (generation !== this.generation) return;
      await this.stop();
      this.onState("error");
      this.onError(
        error instanceof Error ? error.message : "Voice startup failed",
      );
    }
  }
  clearForEpoch(epoch: number) {
    if (this.epoch < epoch) this.clear();
  }
  clearPlaybackResponse(responseId?: string) {
    if (responseId) {
      this.stoppedResponses.add(responseId);
      if (this.activeResponse && this.activeResponse !== responseId) return;
    }
    this.resetPlayback();
  }
  private resetPlayback() {
    this.activeResponse = "";
    this.node?.port.postMessage({
      type: "reset",
      epoch: this.epoch,
      suppressed: this.suppressed,
    });
  }
  stopPlayback() {
    const response = this.activeResponse || null;
    if (response) this.stoppedResponses.add(response);
    this.resetPlayback();
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        JSON.stringify({
          type: "portal.playback.stop",
          epoch: this.epoch,
          payload: { response_id: response },
        }),
      );
    }
  }
  private async rotate(expectedGeneration: number) {
    if (expectedGeneration !== this.generation || !this.cid) return;
    const cid = this.cid;
    if (this.input.kind === "file") {
      await this.stop();
      this.onError(
        "The file call reached its session limit. Start a new file call to run it again.",
      );
      return;
    }
    this.onState("reconnecting");
    await this.stop();
    await this.start(cid);
  }
  clear() {
    this.suppressed = true;
    this.resetPlayback();
  }
  setMuted(value: boolean) {
    if (this.input.kind === "file") value = false;
    this.muted = value;
    this.media?.getAudioTracks().forEach((t) => (t.enabled = !value));
    this.node?.port.postMessage({ type: "mute", value });
  }
  setVolume(value: number) {
    this.volume = value;
    this.node?.port.postMessage({ type: "volume", value });
  }
  async stop() {
    if (!this.recordingStopped) {
      this.recordingStopped = true;
      this.emitRecording(this.fileComplete ? "ended" : "incomplete");
    }
    ++this.generation;
    this.ready = false;
    this.stoppedResponses.clear();
    this.clear();
    const cid = this.cid,
      sid = this.sid;
    this.sid = "";
    this.ws?.close();
    this.ws = undefined;
    if (this.fileSource) {
      const source = this.fileSource;
      this.fileSource = undefined;
      source.onended = null;
      if (this.fileStarted) {
        try {
          source.stop();
        } catch {
          // The source may already have ended naturally.
        }
      }
      source.disconnect();
      source.buffer = null;
    }
    this.node?.disconnect();
    this.node = undefined;
    this.media?.getTracks().forEach((t) => t.stop());
    this.media = undefined;
    this.input = { kind: "microphone" };
    if (this.context) {
      const context = this.context;
      this.context = undefined;
      await context.close().catch(() => {});
    }
    document.removeEventListener("visibilitychange", this.visibility);
    window.removeEventListener("offline", this.offline);
    this.onState("closed");
    if (cid && sid)
      await api(`/conversations/${cid}/voice-sessions/${sid}`, {
        method: "DELETE",
      }).catch(() => {});
  }
}
