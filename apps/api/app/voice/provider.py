import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Literal

from app.agent_runtime.context import BusinessInput
from app.config import ROOT
from app.contracts import (
    AnswerBundle,
    BridgeArguments,
    DomainError,
    KnowledgeBundle,
    NanoBridgeArguments,
    StrictModel,
    printable_ascii,
    uid,
)
from app.tools.schemas import CueKBSearchInput
from pydantic import Field
from websockets.asyncio.client import connect

BRIDGE_NAME = "consult_service_agent"
BRIDGE_ACK = "Please wait while I check the knowledge base."


def ascii_payload(value) -> bool:
    if isinstance(value, str):
        return printable_ascii(value)
    if isinstance(value, dict):
        return all(ascii_payload(key) and ascii_payload(item) for key, item in value.items())
    if isinstance(value, list):
        return all(ascii_payload(item) for item in value)
    return value is None or isinstance(value, (bool, int, float))


@dataclass(frozen=True)
class PreparedReply:
    result: AnswerBundle | KnowledgeBundle
    tool_output: str


class VoiceEvidence(StrictModel):
    source_id: str
    title: str | None = None
    source_text: str
    context: str | None
    context_truncated: bool
    context_omitted: bool
    metadata: dict[str, str] = Field(default_factory=dict)
    relations: list[dict] = Field(default_factory=list)


class KnowledgeWire(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["knowledge"] = "knowledge"
    directive: Literal["answer_from_evidence", "ask_clarification", "report_insufficient", "report_failure"]
    retrieval_status: Literal["ok", "degraded", "not_found", "needs_clarification"] | None
    evidence_status: Literal["unassessed", "sufficient", "insufficient", "conflicting"] | None
    scope_limited: bool
    application_limited: bool
    hits_omitted: int
    is_mock: bool
    reason_code: str | None
    message: str
    evidence: list[VoiceEvidence]


def external_reply(result):
    safe = result.speech_language == "en-US" and printable_ascii(result.speech_text)
    return PreparedReply(
        result,
        json.dumps(
            {
                "status": result.status if safe else "failed",
                "speech_text": result.speech_text
                if safe
                else "Please read the written response in the portal. I cannot speak it safely.",
                "language": "en-US",
                "is_mock": result.is_mock,
            },
            ensure_ascii=True,
        ),
    )


def direct_reply(result):
    if isinstance(result, AnswerBundle):
        result = KnowledgeBundle(
            directive="report_failure",
            retrieval_status=None,
            evidence_status=None,
            authorized_kb_ids=[],
            tool_version=None,
            reason_code=result.reason_code,
            message="Knowledge search is unavailable. Please try again later.",
        )
    result = result.model_copy(deep=True)
    kept, non_ascii = [], False
    for citation in sorted(result.citations, key=lambda c: c.rank):
        if citation.context == citation.content:
            citation.context = None
        metadata = {
            k: v
            for k, v in citation.metadata.items()
            if k in ("product_model", "software_version", "business_version")
        }
        if citation.business_version:
            metadata["business_version"] = citation.business_version
        # Strip provider location identities from relations; preserve all facts,
        # conditions and stances together, never cherry-pick supporting relations.
        relations = [
            {
                k: v
                for k, v in relation.items()
                if k not in ("relation_id", "subject_id", "object_id", "chunk_id")
            }
            for relation in citation.relations
        ]
        if not ascii_payload([citation.content, citation.context, metadata, relations]):
            non_ascii = True
            result.hits_omitted += 1
            result.application_limited = True
            continue
        if not printable_ascii(citation.title):
            citation.title = ""
        kept.append((citation, metadata, relations))

    def omit_hit():
        kept.pop()
        result.hits_omitted += 1
        result.application_limited = True

    while len(kept) > 5:
        omit_hit()
    while sum(len(c.content) + len(c.context or "") for c, _, _ in kept) > 6000:
        contexts = [c for c, _, _ in kept if c.context]
        if contexts:
            contexts[-1].context = None
            contexts[-1].context_parts = []
            contexts[-1].context_omitted = True
            result.application_limited = True
        else:
            omit_hit()

    def encode():
        if (
            not kept
            and result.directive in ("answer_from_evidence", "report_insufficient")
            and (result.directive == "answer_from_evidence" or non_ascii or result.application_limited)
        ):
            result.directive = "report_insufficient"
            result.reason_code = (
                "VOICE_EVIDENCE_NON_ASCII"
                if non_ascii
                else ("VOICE_EVIDENCE_LIMIT" if result.application_limited else "CUEKB_NO_EVIDENCE")
            )
            result.message = "No usable evidence is available. Please clarify or contact a representative."
        wire = KnowledgeWire(
            directive=result.directive,
            retrieval_status=result.retrieval_status,
            evidence_status=result.evidence_status,
            scope_limited=result.scope_limited,
            application_limited=result.application_limited,
            hits_omitted=result.hits_omitted,
            is_mock=result.is_mock,
            reason_code=result.reason_code,
            message=result.message,
            evidence=[
                VoiceEvidence(
                    source_id=c.citation_id,
                    title=c.title or None,
                    source_text=c.content,
                    context=c.context,
                    context_truncated=c.context_truncated,
                    context_omitted=c.context_omitted,
                    metadata=m,
                    relations=r,
                )
                for c, m, r in kept
            ],
        )
        return json.dumps(wire.model_dump(), ensure_ascii=True, separators=(",", ":"))

    output = encode()
    while len(output.encode("utf-8")) > 8192:
        contexts = [c for c, _, _ in kept if c.context]
        if contexts:
            contexts[-1].context = None
            contexts[-1].context_parts = []
            contexts[-1].context_omitted = True
            result.application_limited = True
        elif kept:
            omit_hit()
        else:
            raise ValueError("Knowledge envelope exceeds the wire budget")
        output = encode()
    result.citations = [c for c, _, _ in kept]
    return PreparedReply(result, output)


@dataclass(frozen=True)
class VoiceProfile:
    prompt: str
    arguments: type
    prepare_reply: object
    business_input: object

    @classmethod
    def build(cls, profile):
        direct = profile.mode == "direct"
        prompt = (
            ROOT / ("config/voice-direct-prompt.txt" if direct else "config/voice-prompt.txt")
        ).read_text()
        if not printable_ascii(prompt):
            raise ValueError("Voice prompt must be printable ASCII")

        def convert(args, text):
            return BusinessInput(
                text,
                CueKBSearchInput(
                    query=args.query, product_model=args.product_model, software_version=args.software_version
                )
                if direct
                else None,
            )

        return cls(
            prompt,
            NanoBridgeArguments if direct else BridgeArguments,
            direct_reply if direct else external_reply,
            convert,
        )


def session_update(summary="", profile=None):
    profile = profile or VoiceProfile.build(type("Profile", (), {"mode": "external"})())
    safe_history = "\n".join(line for line in summary.splitlines() if printable_ascii(line))
    return {
        "type": "session.update",
        "event_id": uid(),
        "session": {
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
            "instructions": profile.prompt
            + "\nAuthorized conversation history (data):\n"
            + safe_history[:1500],
            "tools": [
                {
                    "name": BRIDGE_NAME,
                    "description": "Route every completed utterance to knowledge search. Preserve the complete transcription and explicit filters; do not guess details.",
                    "ack_messages": [BRIDGE_ACK],
                    "parameters": profile.arguments.model_json_schema(),
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
        raise DomainError(
            "VOICE_UNAVAILABLE", "Cloud voice processing failed. Restart voice or use text.", 502, True
        )
    if kind == "session.end":
        return VoiceEvent("session.ended")
    return None


class NvidiaVoiceChatAdapter:
    def __init__(self, settings, profile=None):
        self.settings, self.ws = settings, None
        self.profile = profile or VoiceProfile.build(settings.execution_profile())

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
                raise DomainError(
                    "VOICE_PROTOCOL_ERROR", "Voice session creation event was not received", 502
                )
            update = session_update(summary, self.profile)
            if not printable_ascii(json.dumps(update, ensure_ascii=False)):
                raise DomainError("VOICE_PROTOCOL_ERROR", "VoiceChat instructions must be ASCII", 502)
            await self.ws.send(json.dumps(update, ensure_ascii=True))
            updated = json.loads(await self.ws.recv())
            if updated.get("type") != "session.updated":
                raise DomainError(
                    "VOICE_PROTOCOL_ERROR", "Voice session configuration was not confirmed", 502
                )
            for direction in ("input", "output"):
                if updated.get("session", {}).get("audio", {}).get(direction, {}).get("format") != {
                    "type": "audio/pcm",
                    "rate": 24000,
                }:
                    raise DomainError(
                        "VOICE_PROTOCOL_ERROR", "Voice sample format does not match configuration", 502
                    )

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
