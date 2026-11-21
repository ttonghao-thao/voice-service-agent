"""Exercise the official GPT-Live client through a real LiveKit room."""

import argparse
import asyncio
import json
import os
import time
import uuid
import wave
from contextlib import aclosing
from pathlib import Path
from urllib.parse import urlsplit

from app.voice.sophon_gpt_live import sophon_gpt_live
from livekit import api, rtc
from livekit.agents import Agent, AgentSession, room_io

RATE = 24000
FRAME_SAMPLES = RATE // 50


class ProbeAgent(Agent):
    def __init__(self, language, expected_transcript, expected_reply):
        instructions = {
            "zh": "你是语音接口测试助手。用户询问校验码时发起客户端委派，收到结果后口述校验码。其他问题简短回答。",
            "en": "You are testing a voice interface. Delegate requests for a verification code to the client, "
                  "then speak the code from the result. Answer other questions briefly.",
        }
        super().__init__(instructions=instructions[language])
        self.commentary = {
            "zh": "本次语音测试的校验码是六四二九。请口述校验码。",
            "en": "The verification code for this voice test is six four two nine. Say the code aloud.",
        }[language]
        self.expected_transcript = expected_transcript
        self.expected_reply = expected_reply
        self.output_tail = ""
        self.started_at = time.monotonic()
        self.ready = asyncio.Event()
        self.receipt = asyncio.Event()
        self.wire_delegations = {}
        self.delegations = {}
        self.commentary_events = {}
        self.report = {
            "delegations": [], "errors": [], "audio_rate": None, "close_confirmed": False,
            "input_transcript_events": 0, "output_transcript_events": 0,
        }
        if expected_reply:
            self.report["output_transcript_matches"] = False

    def elapsed_ms(self):
        return round((time.monotonic() - self.started_at) * 1000)

    async def on_enter(self):
        duplex = self.duplex_session

        @duplex.on("openai_server_event_received")
        def received(event):
            kind = event["type"]
            if kind == "session.started":
                session = event["session"]
                self.report["upstream_session_id"] = session.get("id")
                self.report["audio_rate"] = session.get("audio", {}).get("format", {}).get("rate")
                self.report["client_delegation"] = session.get("delegation", {}).get("type") == "client"
                self.report["store_disabled"] = session.get("store") is False
                self.ready.set()
            elif kind == "session.input_transcript.delta":
                self.report["input_transcript_events"] += 1
            elif kind == "session.output_transcript.delta":
                self.report["output_transcript_events"] += 1
                if self.expected_reply and not self.report["output_transcript_matches"]:
                    text = self.output_tail + event["delta"]
                    self.report["output_transcript_matches"] = self.expected_reply in text
                    self.output_tail = text[-len(self.expected_reply):]
            elif kind == "session.delegation.created":
                delegation = event["delegation"]
                task = delegation.get("task")
                self.wire_delegations[delegation["id"]] = {
                    "task_on_wire": bool(task),
                    "task_matches": isinstance(task, str) and self.expected_transcript in task,
                }
            elif kind == "session.commentary.appended":
                delivery = self.commentary_events.get(event.get("client_event_id"))
                if delivery is not None:
                    delivery["gateway_receipt_ms"] = self.elapsed_ms()
                    self.receipt.set()
            elif kind == "session.closed":
                self.report["close_confirmed"] = event["session"].get("id") == self.report["upstream_session_id"]
            elif kind == "error":
                self.report["errors"].append(event.get("error", {}).get("code", "unknown"))

        @duplex.on("openai_client_event_queued")
        def queued(event):
            if event["type"] == "session.commentary.append":
                delivery = self.delegations.get(event.get("delegation_id"))
                if delivery is not None:
                    self.commentary_events[event["event_id"]] = delivery

        @duplex.on("delegation_created")
        def delegated(delegation):
            if delegation.id in self.delegations:
                return
            delivery = {
                "id": delegation.id,
                "received_ms": self.elapsed_ms(),
                "transcript_chars": len(delegation.pending_transcript),
                "transcript_matches": self.expected_transcript in delegation.pending_transcript,
                **self.wire_delegations.get(delegation.id, {}),
                "task_on_sdk_object": hasattr(delegation, "task"),
            }
            self.delegations[delegation.id] = delivery
            self.report["delegations"].append(delivery)
            duplex.append_commentary(self.commentary, delegation_id=delegation.id)


def read_wav(path):
    with wave.open(str(path), "rb") as recording:
        if (recording.getframerate(), recording.getsampwidth(), recording.getnchannels()) != (RATE, 2, 1):
            raise ValueError("Expected mono PCM16 WAV at 24 kHz.")
        if not 0 < recording.getnframes() <= RATE * 15:
            raise ValueError("Expected an input recording of up to 15 seconds.")
        return recording.readframes(recording.getnframes())


async def play(source, pcm):
    for start in range(0, len(pcm), FRAME_SAMPLES * 2):
        frame = pcm[start:start + FRAME_SAMPLES * 2].ljust(FRAME_SAMPLES * 2, b"\x00")
        await source.capture_frame(rtc.AudioFrame(frame, RATE, 1, FRAME_SAMPLES))
    await source.wait_for_playout()


async def probe(args):
    pcm = read_wav(args.wav)
    barge = read_wav(args.barge_wav) if args.barge_wav else None
    endpoint = os.environ["SOPHON_LIVE_WS_URL"]
    if urlsplit(endpoint).path != "/v1/live/sessions":
        raise ValueError("SOPHON_LIVE_WS_URL must end in /v1/live/sessions.")
    async with sophon_gpt_live(endpoint, os.environ.get("SOPHON_LIVE_API_KEY")) as model:
        return await room_probe(args, model, pcm, barge)


async def room_probe(args, model, pcm, barge):
    room_name = "voice-probe-" + uuid.uuid4().hex
    agent_room, caller = rtc.Room(), rtc.Room()
    source = rtc.AudioSource(RATE, 1, queue_size_ms=100)
    # The caller publishes WAV frames directly. There is no acoustic echo path.
    session = AgentSession(llm=model, aec_warmup_duration=None)
    agent = ProbeAgent(args.language, args.expected_transcript, args.expected_reply)
    report = agent.report
    output = bytearray()
    post_receipt_output = asyncio.Event()
    receiver = None

    async def receive(track):
        async with aclosing(rtc.AudioStream(track, sample_rate=RATE, num_channels=1, capacity=64)) as stream:
            async for event in stream:
                output.extend(event.frame.data.cast("B"))
                if max(map(abs, event.frame.data), default=0) > 500:
                    report.setdefault("first_active_room_audio_ms", agent.elapsed_ms())
                    if agent.receipt.is_set():
                        report.setdefault("first_active_room_audio_after_receipt_ms", agent.elapsed_ms())
                        post_receipt_output.set()

    @caller.on("track_subscribed")
    def subscribed(track, _publication, participant):
        nonlocal receiver
        if participant.identity == "agent" and track.kind == rtc.TrackKind.KIND_AUDIO and receiver is None:
            receiver = asyncio.create_task(receive(track))

    try:
        async with asyncio.timeout(50):
            for room, identity in ((caller, "caller"), (agent_room, "agent")):
                token = api.AccessToken().with_identity(identity).with_grants(
                    api.VideoGrants(room_join=True, room=room_name)).to_jwt()
                await room.connect(os.environ["LIVEKIT_URL"], token)
            await caller.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("probe-input", source),
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            await session.start(
                room=agent_room, agent=agent, session_host=False, record=False,
                room_options=room_io.RoomOptions(participant_identity="caller"),
            )
            await asyncio.wait_for(agent.ready.wait(), 12)
            report["first_room_input_ms"] = agent.elapsed_ms()
            await play(source, pcm)
            if barge:
                await asyncio.wait_for(post_receipt_output.wait(), 12)
                report["barge_input_ms"] = agent.elapsed_ms()
                await play(source, barge)
            await asyncio.sleep(args.observe_seconds)
    except Exception as exc:
        report["probe_error_type"] = type(exc).__name__
    finally:
        await session.aclose()
        await agent_room.disconnect()
        await caller.disconnect()
        await source.aclose()
        if receiver is not None:
            receiver.cancel()
            result, = await asyncio.gather(receiver, return_exceptions=True)
            if isinstance(result, Exception):
                report["audio_reader_error_type"] = type(result).__name__

    if args.output and output:
        with args.output.open("xb") as stream, wave.open(stream, "wb") as recording:
            recording.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            recording.writeframes(output)
    report["room_output_samples"] = len(output) // 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return (
        report["audio_rate"] == RATE and report.get("client_delegation") and report.get("store_disabled")
        and post_receipt_output.is_set() and report["close_confirmed"] and not report["errors"]
        and "probe_error_type" not in report and "audio_reader_error_type" not in report
        and report.get("output_transcript_matches", True)
        and any(item["transcript_matches"] and "gateway_receipt_ms" in item for item in report["delegations"])
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--barge-wav", type=Path)
    parser.add_argument("--output", type=Path, help="Write room audio to a new WAV file.")
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--expected-transcript", required=True, help="Expected fragment of the input ASR.")
    parser.add_argument("--observe-seconds", type=int, choices=range(1, 16), default=8)
    parser.add_argument("--expected-reply", help="Expected fragment of the provider output transcript.")
    args = parser.parse_args()
    if not args.expected_transcript.strip():
        parser.error("--expected-transcript must be nonempty.")
    raise SystemExit(0 if asyncio.run(probe(args)) else 1)
