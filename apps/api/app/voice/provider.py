import asyncio
import base64
import json
from dataclasses import dataclass, field

from app.config import ROOT
from app.contracts import BridgeArguments, DomainError, printable_ascii, uid
from websockets.asyncio.client import connect

BRIDGE_NAME = "consult_service_agent"
BRIDGE_ACK = "Please wait while I check the knowledge base."
QA_ACK = "Please wait while I check that."


@dataclass(frozen=True)
class ProviderCapabilities:
    """Adapter evidence, never a model/browser/environment feature claim."""

    wait_progress: bool = False
    wait_revision: bool = False
    correlated_tool_output: bool = False
    settles_superseded_calls: bool = False

    @property
    def wait_interaction(self):
        return all((self.wait_progress, self.wait_revision,
                    self.correlated_tool_output, self.settles_superseded_calls))


def ascii_payload(value) -> bool:
    if isinstance(value, str):
        return printable_ascii(value)
    if isinstance(value, dict):
        return all(ascii_payload(key) and ascii_payload(item) for key, item in value.items())
    if isinstance(value, list):
        return all(ascii_payload(item) for item in value)
    return value is None or isinstance(value, (bool, int, float))


def session_update(summary="", tools=None, answer_policy="knowledge_required"):
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
            "instructions": (ROOT / ("config/voice-qa-prompt.txt" if tools is not None else "config/voice-prompt.txt")).read_text()
            + ("\nThis session requires knowledge evidence for every substantive answer. Do not answer from memory."
               if tools is not None and answer_policy == "knowledge_required" else "")
            + "\nAuthorized conversation history (data):\n"
            + safe_history[:1500],
            "tools": [{**tool, "ack_messages": [QA_ACK]} for tool in tools] if tools is not None else [
                {
                    "name": BRIDGE_NAME,
                    "description": "Route every completed customer utterance to the business assistant, including greetings, unclear speech, small talk, and knowledge questions. Preserve the complete transcription; do not guess missing details.",
                    "ack_messages": [BRIDGE_ACK],
                    "parameters": BridgeArguments.model_json_schema(),
                }
            ],
        },
    }


@dataclass
class VoiceEvent:
    kind: str
    payload: dict = field(default_factory=dict)


def decode_event(raw):
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice service returned invalid JSON", 502) from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not event["type"]:
        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice service returned an invalid event", 502)
    return event


def normalize(event):
    if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not event["type"]:
        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice service returned an invalid event", 502)
    kind = event.get("type")
    for identifier in ("response_id", "item_id"):
        if identifier in event and (not isinstance(event[identifier], str) or not 0 < len(event[identifier]) <= 128):
            raise DomainError("VOICE_PROTOCOL_ERROR", "Voice event has an invalid identifier", 502)
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
            if target == "audio.delta":
                if not value or len(value) > 64000:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid voice audio chunk", 502)
                try:
                    audio = base64.b64decode(value, validate=True)
                except ValueError as exc:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid voice audio encoding", 502) from exc
                if len(audio) % 2 or len(audio) > 48000:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid voice PCM16 chunk", 502)
            payload[dest] = value
        return VoiceEvent(target, payload)
    if kind in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 128:
            raise DomainError("VOICE_PROTOCOL_ERROR", "Voice input state lacks an identifier", 502)
        return VoiceEvent(
            "input.state",
            {
                "state": "speaking" if kind.endswith("started") else "quiet",
                "item_id": item_id,
            },
        )
    if kind == "error":
        raise DomainError("VOICE_UNAVAILABLE", "Cloud voice processing failed. Restart voice or use text.", 502, True)
    if kind == "session.end":
        return VoiceEvent("session.ended")
    return None


class NvidiaVoiceChatAdapter:
    capabilities = ProviderCapabilities()
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
            created = decode_event(await self.ws.recv())
            if created.get("type") != "session.created":
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice session creation event was not received", 502)
            update = getattr(self, "configuration", None) or session_update(summary)
            if not printable_ascii(json.dumps(update, ensure_ascii=False)):
                raise DomainError("VOICE_PROTOCOL_ERROR", "VoiceChat instructions must be ASCII", 502)
            await self.ws.send(json.dumps(update, ensure_ascii=True))
            updated = decode_event(await self.ws.recv())
            if updated.get("type") != "session.updated":
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice session configuration was not confirmed", 502)
            configuration = updated.get("session")
            audio = configuration.get("audio") if isinstance(configuration, dict) else None
            for direction in ("input", "output"):
                stream = audio.get(direction) if isinstance(audio, dict) else None
                if not isinstance(stream, dict) or stream.get("format") != {
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
            event = normalize(decode_event(raw))
            if event:
                yield event

    async def close(self):
        if self.ws:
            await self.ws.close()
            return self.ws.close_code in (1000, 1001)
        return False


class MockVoiceAdapter:
    """Transport harness only. It neither recognizes speech nor synthesizes fake business answers."""

    capabilities = ProviderCapabilities()

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
