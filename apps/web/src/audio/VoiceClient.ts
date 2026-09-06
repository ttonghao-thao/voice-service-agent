import { api, PortalEvent } from "../api";
export type VoiceState =
  "closed" | "connecting" | "ready" | "reconnecting" | "error";
export class VoiceClient {
  private context?: AudioContext;
  private node?: AudioWorkletNode;
  private media?: MediaStream;
  private ws?: WebSocket;
  private cid = "";
  private sid = "";
  private epoch = -1;
  private seq = 0;
  private ready = false;
  private generation = 0;
  private lastCapture = 0;
  private suppressed = true;
  private muted = false;
  private volume = 0.8;
  constructor(
    private onEvent: (e: PortalEvent) => void,
    private onState: (state: VoiceState) => void,
    private onError: (message: string) => void,
  ) {}
  private visibility = () => {
    if (document.hidden) {
      void this.stop();
      this.onError("页面进入后台，语音已停止。返回后请继续语音。");
    }
  };
  private offline = () => {
    void this.stop();
    this.onError("网络已断开，语音播放已停止。");
  };
  async start(cid: string) {
    await this.stop();
    const generation = ++this.generation;
    this.cid = cid;
    this.onState("connecting");
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error(
          "浏览器无法访问麦克风，请使用 HTTPS 或本机 localhost。",
        );
      this.context = new AudioContext();
      await this.context.resume();
      this.media = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (generation !== this.generation) {
        this.media.getTracks().forEach((t) => t.stop());
        return;
      }
      await this.context.audioWorklet.addModule("/audio-worklet.js");
      this.node = new AudioWorkletNode(this.context, "portal-audio", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      this.context.createMediaStreamSource(this.media).connect(this.node);
      this.node.connect(this.context.destination);
      const session = await api<{
        voice_session_id: string;
        epoch: number;
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
          this.onError("音频播放积压，已停止，请重新开始语音。");
          return;
        }
        if (
          !this.ready ||
          this.ws?.readyState !== WebSocket.OPEN ||
          d.epoch !== this.epoch
        )
          return;
        if (d.type === "capture") {
          const clock = performance.now();
          if (this.lastCapture && clock - this.lastCapture > 500) {
            void this.stop();
            this.onError("音频采集发生停顿，请重新开始语音。");
            return;
          }
          this.lastCapture = clock;
          if (this.ws.bufferedAmount > 32000) {
            void this.stop();
            this.onError("网络音频积压，请重新开始语音。");
            return;
          }
          const bytes = new Uint8Array(d.pcm);
          let binary = "";
          for (const b of bytes) binary += String.fromCharCode(b);
          this.ws.send(
            JSON.stringify({
              type: "portal.audio.append",
              event_id: crypto.randomUUID(),
              epoch: this.epoch,
              seq: this.seq++,
              payload: {
                format: "pcm16",
                sample_rate: 24000,
                audio: btoa(binary),
              },
            }),
          );
        } else if (d.type === "ack" && !this.suppressed) {
          this.ws.send(
            JSON.stringify({
              type: "portal.playback.ack",
              epoch: this.epoch,
              payload: { response_id: d.response, played_samples: d.samples },
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
          this.onError("语音事件格式错误");
          return;
        }
        if (event.epoch !== this.epoch) return;
        if (event.type === "portal.session.ready") {
          this.ready = true;
          this.suppressed = false;
          this.lastCapture = 0;
          this.node?.port.postMessage({ type: "ready", epoch: this.epoch });
          this.onState("ready");
        }
        if (event.type === "portal.audio.delta" && !this.suppressed) {
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
        if (event.type === "portal.audio.done")
          this.node?.port.postMessage({
            type: "done",
            epoch: this.epoch,
            response: event.payload.response_id,
          });
        if (event.type === "portal.error") {
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
        this.clear();
        this.onError("语音连接失败，仍可使用文字问答。");
        void this.stop();
      };
      document.addEventListener("visibilitychange", this.visibility);
      window.addEventListener("offline", this.offline);
    } catch (error) {
      await this.stop();
      this.onState("error");
      this.onError(error instanceof Error ? error.message : "语音启动失败");
    }
  }
  clearForEpoch(epoch: number) {
    if (this.epoch < epoch) this.clear();
  }
  clear() {
    this.suppressed = true;
    this.node?.port.postMessage({ type: "reset", epoch: this.epoch });
  }
  setMuted(value: boolean) {
    this.muted = value;
    this.media?.getAudioTracks().forEach((t) => (t.enabled = !value));
    this.node?.port.postMessage({ type: "mute", value });
  }
  setVolume(value: number) {
    this.volume = value;
    this.node?.port.postMessage({ type: "volume", value });
  }
  async stop() {
    ++this.generation;
    this.ready = false;
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
