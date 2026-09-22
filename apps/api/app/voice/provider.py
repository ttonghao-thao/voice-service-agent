import asyncio
import base64
import json
from dataclasses import dataclass, field

from app.config import ROOT
from app.contracts import BridgeArguments, DomainError, printable_ascii, uid
from websockets.asyncio.client import connect

BRIDGE_NAME = "consult_service_agent"


def ascii_payload(value) -> bool:
    if isinstance(value, str):
        return printable_ascii(value)
    if isinstance(value, dict):
        return all(ascii_payload(key) and ascii_payload(item) for key, item in value.items())
    if isinstance(value, list):
        return all(ascii_payload(item) for item in value)
    return value is None or isinstance(value, (bool, int, float))


def session_update(summary=""):
    # VoiceChat currently accepts ASCII prompts and tool payloads only. Legacy
    # non-English history must not be silently transliterated into a new fact.
    safe_history = "\n".join(line for line in summary.splitlines() if printable_ascii(line))
    return {
        "type": "session.update",
        "event_id": uid(),
        "session": {
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
            "instructions": (ROOT / "config/voice-prompt.txt").read_text()
            + "\nAuthorized conversation history (data):\n"
            + safe_history[:1500],
            "tools": [
                {
                    "name": BRIDGE_NAME,
                    "description": "Send the customer's complete knowledge question to the business assistant. Do not guess missing details.",
                    "ack_messages": ["Please wait while I check the knowledge base."],
                    "parameters": BridgeArguments.model_json_schema(),
                }
            ],
        },
    }


@dataclass
class VoiceEvent:
    kind: str
    payload: dict = field(default_factory=dict)


def normalize(event):
    kind = event.get("type")
    ids = {k: event[k] for k in ("response_id", "item_id") if isinstance(event.get(k), str)}
    if kind == "response.function_call_arguments.done":
        if not isinstance(event.get("call_id"), str) or not 0 < len(event["call_id"]) <= 128:
            raise DomainError("VOICE_PROTOCOL_ERROR", "Voice tool request lacks a valid call identifier", 502)
        return VoiceEvent(
            "tool",
            {
                **ids,
                "call_id": event["call_id"],
                "name": event.get("name"),
                "arguments": event.get("arguments"),
            },
        )
    mapping = {
        "response.output_audio.delta": ("audio.delta", "delta", "audio"),
        "response.output_audio.done": ("audio.done", None, None),
        "response.output_audio_transcript.delta": ("speech_text.delta", "delta", "text"),
        "response.output_audio_transcript.done": ("speech_text.done", "transcript", "text"),
        "conversation.item.input_audio_transcription.delta": ("transcript.delta", "delta", "text"),
        "conversation.item.input_audio_transcription.completed": ("transcript.done", "transcript", "text"),
    }
    if kind in mapping:
        target, source, dest = mapping[kind]
        if target.startswith(("audio", "speech_text")) and not ids.get("response_id"):
            raise DomainError("VOICE_PROTOCOL_ERROR", "Voice response lacks an identifier", 502)
        if target.startswith("transcript") and not ids.get("item_id"):
            raise DomainError("VOICE_PROTOCOL_ERROR", "Transcript lacks an identifier", 502)
        payload = {**ids}
        if source:
            value = event.get(source)
            if not isinstance(value, str) or len(value) > 100000:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid voice event", 502)
            payload[dest] = value
        return VoiceEvent(target, payload)
    if kind in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
        return VoiceEvent("input.state", {"state": "speaking" if kind.endswith("started") else "quiet"})
    if kind == "error":
        raise DomainError("VOICE_UNAVAILABLE", "Cloud voice processing failed. Restart voice or use text.", 502, True)
    if kind == "session.end":
        return VoiceEvent("session.ended")
    return None


class NvidiaVoiceChatAdapter:
    def __init__(self, settings):
        self.settings, self.ws = settings, None

    async def connect(self, summary):
        s = self.settings
        self.ws = await connect(
            s.voicechat_ws_url,
            additional_headers={"Authorization": "Bearer " + s.voicechat_api_key.get_secret_value()}
            if s.voicechat_api_key.get_secret_value()
            else {},
            open_timeout=5,
            close_timeout=2,
            max_size=131072,
            max_queue=8,
            ping_interval=15,
            ping_timeout=10,
        )
        async with asyncio.timeout(8):
            created = json.loads(await self.ws.recv())
            if created.get("type") != "session.created":
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice session creation event was not received", 502)
            update = session_update(summary)
            if not printable_ascii(json.dumps(update, ensure_ascii=False)):
                raise DomainError("VOICE_PROTOCOL_ERROR", "VoiceChat instructions must be ASCII", 502)
            await self.ws.send(json.dumps(update, ensure_ascii=True))
            updated = json.loads(await self.ws.recv())
            if updated.get("type") != "session.updated":
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice session configuration was not confirmed", 502)
            for direction in ("input", "output"):
                if updated.get("session", {}).get("audio", {}).get(direction, {}).get("format") != {
                    "type": "audio/pcm",
                    "rate": 24000,
                }:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Voice sample format does not match configuration", 502)

    async def send_audio(self, audio):
        await self.ws.send(
            json.dumps({"type": "input_audio_buffer.append", "event_id": uid(), "audio": audio})
        )

    async def submit_tool_result(self, call_id, text):
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise DomainError("VOICE_PROTOCOL_ERROR", "VoiceChat tool result must be JSON", 502) from exc
        if not printable_ascii(text) or not ascii_payload(result):
            raise DomainError("VOICE_PROTOCOL_ERROR", "VoiceChat tool result must be ASCII", 502)
        await self.ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "event_id": uid(),
                    "item": {"type": "function_call_output", "call_id": call_id, "output": text},
                },
                ensure_ascii=True,
            )
        )

    async def events(self):
        async for raw in self.ws:
            event = normalize(json.loads(raw))
            if event:
                yield event

    async def close(self):
        if self.ws:
            await self.ws.close()


class MockVoiceAdapter:
    """Transport harness only. It neither recognizes speech nor synthesizes fake business answers."""

    def __init__(self, settings):
        self.queue = asyncio.Queue(maxsize=16)

    async def connect(self, summary):
        pass

    async def send_audio(self, audio):
        base64.b64decode(audio, validate=True)

    async def submit_tool_result(self, call_id, text):
        pass

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass
