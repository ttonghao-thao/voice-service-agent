"""Sophon Live wire protocol. Portal audio remains mono PCM16 at 24 kHz."""

import asyncio
import base64
import json
import logging
import re

from app.contracts import DomainError, uid
from app.voice.profiles import default_profile, instructions
from app.voice.provider import VoiceEvent, decode_event
from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)
RATE = 24000
ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def protocol_error():
    return DomainError("VOICE_PROTOCOL_ERROR", "Sophon Live returned an invalid event.", 502)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise protocol_error()
    return value


def timestamp(value):
    if type(value) is not int or not 0 <= value <= 7_200_000:
        raise protocol_error()
    return value


class SophonLiveAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.ws = None
        self.session_id = None
        self.close_confirmed = False
        self.unknown_events = set()
        self.instructions = None

    async def connect(self):
        prompt = self.instructions or instructions(default_profile(), self.settings.sophon_live_locale, livekit=False)
        key = self.settings.sophon_live_api_key.get_secret_value()
        try:
            async with asyncio.timeout(15):
                self.ws = await connect(
                    self.settings.sophon_live_ws_url,
                    additional_headers={"Authorization": "Bearer " + key} if key else None,
                    proxy=None, max_size=32768, max_queue=8, close_timeout=1,
                )
                await self.send("session.start", session={
                    "model": "gpt-live-1", "instructions": prompt,
                    "audio": {"format": {"type": "audio/pcm", "rate": RATE}, "output": {"voice": "chelsie"}},
                    "delegation": {"type": "client"}, "store": False,
                })
                event = decode_event(await self.ws.recv())
        except TimeoutError as exc:
            raise DomainError("VOICE_HANDSHAKE_TIMEOUT", "Sophon Live did not start in time.", 504, True) from exc
        if event["type"] == "error":
            raise self.upstream_error(event)
        session = event.get("session")
        if (event["type"] != "session.started" or not isinstance(session, dict)
            or session.get("status") != "active" or session.get("store") is not False
            or session.get("delegation") != {"type": "client"}
            or not isinstance(session.get("audio"), dict)
            or session["audio"].get("format") != {"type": "audio/pcm", "rate": RATE}):
            raise protocol_error()
        self.session_id = identifier(session.get("id"))

    async def send(self, kind, **payload):
        await self.ws.send(json.dumps({"type": kind, **payload}, ensure_ascii=False))

    async def send_audio(self, audio):
        await self.send("session.input_audio.append", audio=audio)

    async def submit_result(self, delegation_id, content, *, event_id=None):
        event_id = event_id or uid()
        await self.send("session.commentary.append", event_id=event_id, delegation_id=delegation_id, content=content)
        return event_id

    @staticmethod
    def upstream_error(event):
        error = event.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        logger.warning("sophon_upstream_error code=%s", code if isinstance(code, str) and ID.fullmatch(code) else "unknown")
        return DomainError("VOICE_PROVIDER_ERROR", "Sophon Live reported an error.", 502, True)

    def confirm_closed(self, event):
        session = event.get("session")
        if not isinstance(session, dict) or session.get("id") != self.session_id:
            raise protocol_error()
        self.close_confirmed = True

    async def events(self):
        async for raw in self.ws:
            event = decode_event(raw)
            kind = event["type"]
            if kind == "session.output_audio.delta":
                try:
                    pcm = base64.b64decode(event.get("delta"), validate=True)
                except (TypeError, ValueError) as exc:
                    raise protocol_error() from exc
                if not pcm or len(pcm) % 2 or len(pcm) > RATE * 2 // 5:
                    raise protocol_error()
                yield VoiceEvent("audio", {"pcm": pcm, "audio": event["delta"]})
            elif kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                text = event.get("delta")
                if kind.startswith("session.input") and (not isinstance(text, str) or not text):
                    continue
                start, end = timestamp(event.get("start_ms")), timestamp(event.get("end_ms"))
                if (not isinstance(text, str) or not 0 < len(text) <= 2000
                    or any(not char.isprintable() and char not in "\r\n\t" for char in text) or end <= start):
                    raise protocol_error()
                yield VoiceEvent("input_text" if kind.startswith("session.input") else "output_text",
                                 {"text": text, "start": start, "end": end})
            elif kind == "session.delegation.created":
                body = event.get("delegation")
                if not isinstance(body, dict) or body.get("type") != "delegation" or body.get("target") != "client":
                    raise protocol_error()
                task = body.get("task")
                if task is not None and (not isinstance(task, str) or len(task) > 2000):
                    raise protocol_error()
                # The official Gateway can forward a missing engine task as
                # null. The relay can settle that ID without executing a tool.
                yield VoiceEvent("delegation", {"id": identifier(body.get("id")), "task": task or "",
                                                "offset": timestamp(event.get("offset_ms"))})
            elif kind == "session.commentary.appended":
                yield VoiceEvent("ack", {"event_id": identifier(event.get("client_event_id")),
                                        "offset": timestamp(event.get("end_ms"))})
            elif kind == "session.closed":
                self.confirm_closed(event)
                return
            elif kind == "error":
                raise self.upstream_error(event)
            elif kind == "session.started":
                raise protocol_error()
            elif len(self.unknown_events) < 16 and kind not in self.unknown_events:
                self.unknown_events.add(kind)
                logger.info("sophon_event_ignored type=%s", kind if ID.fullmatch(kind) else "unknown")

    async def close(self):
        if self.ws is None:
            return False
        try:
            if self.session_id and not self.close_confirmed:
                await self.send("session.close")
                async with asyncio.timeout(1):
                    async for raw in self.ws:
                        event = decode_event(raw)
                        if event["type"] == "session.closed":
                            self.confirm_closed(event)
                            break
        except TimeoutError:
            pass
        finally:
            await self.ws.close()
        return self.close_confirmed
