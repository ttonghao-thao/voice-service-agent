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
    | "reply_complete"
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
  private fileId = 0;
  private fileArmed = false;
  private diagnosticAudio = new Set<string>();
  private diagnosticText = new Set<string>();
  private fileSourceEnded = false;
  private sentFileSamples = 0;
  private fileComplete = false;
  private fileInputs = new Set<string>();
  private knownFileInputs = new Set<string>();
  private fileAnswerResponses = new Set<string>();
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
  private diagnostic(event: string, fields: Record<string, unknown> = {}) {
    if (this.input.kind !== "file") return;
    console.info(
      "voice_file",
      JSON.stringify({
        at: new Date().toISOString(),
        event,
        conversation_id: this.cid,
        voice_session_id: this.sid,
        epoch: this.epoch,
        ...fields,
      }),
    );
  }
  private sendAudio(bytes: Uint8Array): boolean {
    if (!this.ready || this.ws?.readyState !== WebSocket.OPEN) return false;
    const clock = performance.now();
    if (this.lastCapture && clock - this.lastCapture > 500) {
      void this.stop();
      this.onError("Audio capture stalled. Restart voice.");
      return false;
    }
    this.lastCapture = clock;
    if (this.ws.bufferedAmount > 32000) {
      void this.stop();
      this.onError("Network audio fell behind. Restart voice.");
      return false;
    }
    let binary = "";
    for (const b of bytes) binary += String.fromCharCode(b);
    try {
      this.ws.send(
        JSON.stringify({
          type: "portal.audio.append",
          event_id: crypto.randomUUID(),
          epoch: this.epoch,
          seq: this.seq,
          payload: { format: "pcm16", sample_rate: 24000, audio: btoa(binary) },
        }),
      );
      this.seq++;
      return true;
    } catch {
      void this.stop();
      this.onError("Voice audio could not be sent. Restart the call.");
      return false;
    }
  }
  private validateRecording(recording: PreparedRecording) {
    if (!recording.sampleCount || recording.sampleCount * 2 !== recording.pcm.length ||
      recording.sampleCount > 720000 || recording.sampleRate !== 24000)
      throw new Error("Choose a validated recording no longer than 30 seconds.");
  }
  private armRecording() {
    if (this.input.kind !== "file" || this.fileArmed) return;
    const recording = this.input.recording;
    this.validateRecording(recording);
    this.fileArmed = true;
    this.fileComplete = false;
    this.fileSourceEnded = false;
    this.fileInputs.clear();
    this.fileAnswerResponses.clear();
    this.sentFileSamples = 0;
    this.recordingStage = undefined;
    this.recordingStopped = false;
    ++this.fileId;
    this.emitRecording("sending");
    this.diagnostic("file.start", {
      name: recording.name, samples: recording.sampleCount, file_id: this.fileId,
      frame_samples: 1920, frame_ms: 80,
    });
    // Transfer a copy so an explicit later send can still use the selected WAV.
    const pcm = recording.pcm.slice();
    this.node!.port.postMessage({ type: "file", epoch: this.epoch,
      id: this.fileId, pcm: pcm.buffer, samples: recording.sampleCount }, [pcm.buffer]);
  }
  sendRecording(recording: PreparedRecording) {
    if (!this.ready || this.input.kind !== "file" || !this.fileComplete || this.ws?.readyState !== WebSocket.OPEN)
      throw new Error("Wait for the current recording to finish sending before sending another.");
    this.validateRecording(recording);
    this.input = { kind: "file", recording };
    this.fileArmed = false;
    this.armRecording();
  }
  private visibility = () => {
    this.diagnostic("page.visibility", { hidden: document.hidden, audio_state: this.context?.state });
    // Visibility is not a request to end the call. Resume audio when possible;
    // real network/capture failures still use the existing transport checks.
    const context = this.context;
    if (!document.hidden && context && context.state !== "running" && context.state !== "closed") {
      void context.resume().catch(() => {
        if (this.context === context && this.ready)
          this.onError("The browser paused audio. Return to the call to resume audio.");
      });
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
    this.diagnosticAudio.clear();
    this.knownFileInputs.clear();
    this.diagnosticText.clear();
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
        processorOptions: { capture: input.kind === "microphone" },
      });
      if (input.kind === "microphone") {
        context.createMediaStreamSource(this.media!).connect(this.node);
      } else {
        this.validateRecording(input.recording);
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
        if (generation !== this.generation) return;
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
        if (d.type === "playback.started") {
          this.diagnostic("playback.started", { response_id: d.response });
        } else if (d.type === "file.frame" && this.input.kind === "file" && d.id === this.fileId) {
          if (!this.sendAudio(new Uint8Array(d.pcm))) return;
          if (!this.fileComplete) {
            this.sentFileSamples = d.sentSamples;
            this.fileComplete = d.sentSamples === this.input.recording.sampleCount;
            this.fileSourceEnded = this.fileComplete;
            this.emitRecording(this.fileComplete ? "waiting_answer" : "sending");
            if (this.fileComplete) this.diagnostic("file.sent", { samples: this.sentFileSamples, file_id: this.fileId });
          }
        } else if (d.type === "capture" && this.input.kind === "microphone") {
          this.sendAudio(new Uint8Array(d.pcm));
        } else if (d.type === "ack" && !this.suppressed) {
          if (d.done && this.fileComplete && this.fileAnswerResponses.has(d.response))
            this.emitRecording("reply_complete");
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
        if (event.type === "portal.speech_text.delta" && event.payload.phase === "answer" && event.payload.text) {
          const response = String(event.payload.response_id || "");
          if (response && !this.diagnosticText.has(response)) {
            this.diagnosticText.add(response);
            this.diagnostic("speech_text.first", { response_id: response, input_item_id: event.payload.input_item_id });
          }
        }
        if (
          [
            "portal.transcript.done",
            "portal.speech_text.done",
            "portal.audio.done",
            "portal.error",
          ].includes(event.type)
        ) {
          const p = event.payload;
          this.diagnostic(event.type, {
            input_item_id:
              event.type === "portal.transcript.done"
                ? p.item_id
                : p.input_item_id,
            output_item_id:
              event.type === "portal.audio.done" ? p.item_id : undefined,
            response_id: p.response_id,
            phase: p.phase,
            code: p.code,
            ...(event.type === "portal.transcript.done" ? {
              transcript_chars: String(p.text || "").length,
              transcript_preview: String(p.text || "").slice(0, 160),
            } : {}),
          });
        }
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
        if (event.type.startsWith("portal.transcript.") && this.input.kind === "file") {
          const id = String(event.payload.item_id);
          if (!this.knownFileInputs.has(id)) this.fileInputs.add(id);
          this.knownFileInputs.add(id);
        }
        if (event.type === "portal.speech_text.done" && event.payload.phase === "answer" && this.fileComplete) {
          const inputs = [event.payload.input_item_id, ...((event.payload.related_input_item_ids || []) as string[])];
          if (inputs.some((id) => this.fileInputs.has(String(id))))
            this.fileAnswerResponses.add(String(event.payload.response_id));
        }
        if (event.type === "portal.audio.delta" && !this.suppressed) {
          const response = String(event.payload.response_id);
          if (!this.diagnosticAudio.has(response)) {
            this.diagnosticAudio.add(response);
            this.diagnostic("audio.first", { response_id: response });
          }
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
          if (
            event.payload.code === "VOICE_SESSION_ROTATION_REQUIRED" ||
            event.payload.code === "VOICE_TOOL_CONFIGURATION_CHANGED"
          ) {
            this.onEvent(event);
            void this.rotate(
              generation,
              event.payload.code === "VOICE_TOOL_CONFIGURATION_CHANGED",
            );
            return;
          }
          this.clear();
          this.onError(
            `${String(event.payload.code || "VOICE_UNAVAILABLE")}: ${String(event.payload.message || "Voice failed. Restart the call or use text.")}`,
          );
          void this.stop();
        }
        this.onEvent(event);
      };
      this.ws.onclose = () => {
        if (generation === this.generation) {
          this.clear();
          this.onError(
            "Voice connection ended before the call was finished. Restart the call or use text. Received transcripts remain in the chat.",
          );
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
  private async rotate(expectedGeneration: number, toolsChanged = false) {
    if (expectedGeneration !== this.generation || !this.cid) return;
    const cid = this.cid;
    if (this.input.kind === "file") {
      await this.stop();
      this.onError(
        toolsChanged
          ? "The voice tools changed. Start a new file call to register them; the recording was not replayed."
          : "The file call reached its session limit. Start a new file call to run it again.",
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
