import { ConnectionError, Room, RoomEvent, Track } from "livekit-client";
import type { PreparedRecording } from "./recordedAudio";

export interface RoomCredentials {
  url: string;
  token: string;
  agent_identity: string;
}

const startupErrors = {
  signal: "Could not connect to the audio room. Check your network and the server certificate. (LIVEKIT_SIGNAL_FAILED)",
  media: "The audio room was reached, but its audio connection could not be established. (LIVEKIT_MEDIA_FAILED)",
  playback: "The browser could not start audio playback. Start a new call. (LIVEKIT_PLAYBACK_FAILED)",
  publish: "The audio room could not send your selected audio. (LIVEKIT_PUBLISH_FAILED)",
};

export class LiveKitMedia {
  private room = new Room();
  private playback?: HTMLMediaElement;
  private fileTrack?: MediaStreamTrack;
  private file?: AudioBufferSourceNode;
  private outputMuted = true;
  private volume = 0.8;

  async connect(credentials: RoomCredentials, context: AudioContext, mic: MediaStreamTrack | undefined, onClose: () => void) {
    let stage: keyof typeof startupErrors = "signal";
    let started = false;
    let disconnectedAt: keyof typeof startupErrors | undefined;
    this.room.once(RoomEvent.SignalConnected, () => { stage = "media"; });
    this.room.on(RoomEvent.Disconnected, () => {
      // The SDK emits Disconnected before rejecting an initial connection.
      if (started) onClose();
      else disconnectedAt ??= stage;
    });
    this.room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
      if (track.kind !== Track.Kind.Audio || participant.identity !== credentials.agent_identity) return;
      this.playback?.remove();
      const audio = track.attach();
      audio.muted = this.outputMuted;
      audio.volume = this.volume;
      this.playback = audio;
      document.body.append(audio);
    });
    this.room.on(RoomEvent.TrackUnsubscribed, (track) => {
      track.detach().forEach((element) => {
        if (element === this.playback) this.playback = undefined;
        element.remove();
      });
    });
    try {
      await this.room.connect(credentials.url, credentials.token);
      stage = "playback";
      await this.room.startAudio();
      stage = "publish";
      if (mic) {
        await this.room.localParticipant.publishTrack(mic, { source: Track.Source.Microphone });
      } else {
        this.destination = context.createMediaStreamDestination();
        this.fileTrack = this.destination.stream.getAudioTracks()[0];
        await this.room.localParticipant.publishTrack(this.fileTrack, { source: Track.Source.Microphone });
      }
    } catch (error) {
      if (stage === "signal" && error instanceof ConnectionError &&
          (error.status === 401 || error.status === 403)) {
        throw new Error("The audio room refused the connection. Start a new call. (LIVEKIT_JOIN_REJECTED)");
      }
      throw new Error(startupErrors[disconnectedAt ?? stage]);
    }
    if (disconnectedAt) throw new Error(startupErrors[disconnectedAt]);
    started = true;
  }

  private destination?: MediaStreamAudioDestinationNode;

  playRecording(context: AudioContext, recording: PreparedRecording, onEnd: () => void) {
    if (this.file || !this.destination) throw new Error("Wait for the current recording to finish.");
    const buffer = context.createBuffer(1, recording.sampleCount, recording.sampleRate);
    const samples = buffer.getChannelData(0);
    const bytes = new DataView(recording.pcm.buffer, recording.pcm.byteOffset, recording.pcm.byteLength);
    for (let i = 0; i < samples.length; i++) samples[i] = bytes.getInt16(i * 2, true) / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.destination);
    this.file = source;
    source.onended = () => {
      source.disconnect();
      if (this.file !== source) return;
      this.file = undefined;
      onEnd();
    };
    source.start();
  }

  muteOutput(muted: boolean) {
    this.outputMuted = muted;
    if (this.playback) this.playback.muted = muted;
  }

  setVolume(volume: number) {
    this.volume = volume;
    if (this.playback) this.playback.volume = volume;
  }

  async close() {
    this.room.removeAllListeners();
    if (this.file) {
      this.file.onended = null;
      this.file.stop();
      this.file.disconnect();
      this.file = undefined;
    }
    this.fileTrack?.stop();
    this.playback?.remove();
    this.playback = undefined;
    await this.room.disconnect();
  }
}
