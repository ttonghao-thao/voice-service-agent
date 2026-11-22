"""Use one official LiveKit AgentSession per authorized Sophon call."""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta

import anyio
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_input import NativeInputSearch
from app.config import ROOT
from app.contracts import (
    DomainError,
    PortalEvent,
    portal_client_event_adapter,
    portal_server_event_adapter,
    uid,
)
from app.sessions.coordinator import SophonEvidenceOutcome
from app.voice.delegation_router import DelegationRouter
from app.voice.sophon_gpt_live import sophon_gpt_live
from app.voice.sophon_live import identifier, timestamp
from app.voice.sophon_relay import commentary, disposition, voiced
from livekit import api, rtc
from livekit.agents import Agent, AgentSession, room_io
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)
CALLER = "caller"
AGENT = "voice-agent"
INPUT_LOOKBACK = 8
SPEECH_PAUSE = 0.7


def room_token(settings, sid, identity):
    return (
        api.AccessToken(settings.livekit_api_key.get_secret_value(), settings.livekit_api_secret.get_secret_value())
        .with_identity(identity).with_ttl(timedelta(seconds=60))
        .with_grants(api.VideoGrants(
            room="voice-" + sid, room_join=True, can_publish=True, can_subscribe=True,
            can_publish_data=identity == AGENT,
            can_publish_sources=["microphone"] if identity == CALLER else None,
        )).to_jwt()
    )


def browser_credentials(settings, sid):
    return {"url": settings.livekit_url, "token": room_token(settings, sid, CALLER), "agent_identity": AGENT}


@dataclass
class SpeechInput:
    id: str
    started: float
    ended: float | None = None
    claimed: bool = False
    transcript: str = ""


class RoomInputs:
    def __init__(self):
        self.recent = deque(maxlen=32)
        self.speaking = False
        self.changed = asyncio.Event()

    @property
    def latest(self):
        return self.recent[-1] if self.recent else None

    def update(self, speaking):
        if speaking == self.speaking:
            return
        self.speaking = speaking
        now = time.monotonic()
        if speaking:
            prior = self.latest
            if prior and prior.ended is not None and now - prior.ended <= SPEECH_PAUSE and not prior.claimed:
                prior.ended = None
            else:
                self.recent.append(SpeechInput(uid(), now))
        elif self.latest:
            self.latest.ended = now
        self.changed.set()

    def at_delegation(self, received):
        candidates = [item for item in self.recent if item.started <= received
                      and (item.ended is None or received - item.ended <= INPUT_LOOKBACK)]
        return candidates[0] if len(candidates) == 1 else None


class RoomAgent(Agent):
    def __init__(self, bridge, instructions):
        super().__init__(instructions=instructions)
        self.bridge = bridge

    async def on_enter(self):
        duplex = self.duplex_session
        self.bridge.duplex = duplex
        duplex.on("openai_server_event_received", self.bridge.receive)
        duplex.on("openai_client_event_queued", self.bridge.sent)
        duplex.on("delegation_created", self.bridge.delegate)
        duplex.on("input_audio_transcription_completed", self.bridge.transcript_finished)


class RoomCall:
    def __init__(self, gateway, ws, session):
        self.gateway, self.ws, self.session = gateway, ws, session
        self.coordinator, self.store, self.settings = gateway.coordinator, gateway.store, gateway.settings
        self.cid, self.epoch = session.conversation_id, session.epoch
        self.chinese = self.settings.sophon_live_locale == "zh-CN"
        self.router = DelegationRouter(self.coordinator.runtime.registry)
        self.room = rtc.Room()
        self.inputs = RoomInputs()
        self.calls = asyncio.Queue(maxsize=8)
        self.outgoing = asyncio.Queue(maxsize=48)
        self.ready, self.closed = asyncio.Event(), asyncio.Event()
        self.duplex = None
        self.error = None
        self.confirmed = False
        self.sent_private_content = False
        self.upstream_id = None
        self.first_audio = False
        self.last_voiced = 0.0
        self.seq = 0
        self.raw_tasks = {}
        self.seen = set()
        self.receipts = {}
        self.receipt_ids = {}
        self.caption_id = uid()
        self.caption_input = None
        self.caption_text = ""
        self.started = time.monotonic()

    def end(self, error=None):
        if error and not self.error:
            self.error = error
        self.closed.set()

    def event(self, kind, payload):
        self.seq += 1
        return PortalEvent(type="portal." + kind, conversation_id=self.cid, epoch=self.epoch,
            request_revision=self.session.request_revision, server_seq=self.seq, payload=payload).model_dump(mode="json")

    def emit(self, kind, payload):
        try:
            self.outgoing.put_nowait(self.event(kind, payload))
        except asyncio.QueueFull:
            self.end(DomainError("VOICE_BACKPRESSURE", "Voice status fell behind. Restart the call.", 409, True))

    async def current(self):
        await self.coordinator.coordination.check(self.cid)
        if self.session.stopped or not await self.store.current(self.cid, self.epoch):
            raise DomainError("STALE_EPOCH", "This voice connection has expired.", 409)
        expires = self.session.principal.expires_at
        if expires is not None and expires <= time.time():
            raise DomainError("AUTH_REQUIRED", "Your session expired. Please sign in again.", 401)

    def finish_caption(self):
        if self.caption_text and self.caption_input:
            self.emit("speech_text.done", {
                "response_id": self.caption_id, "input_item_id": self.caption_input.id,
                "text": self.caption_text, "phase": "candidate",
            })
        self.caption_id, self.caption_text, self.caption_input = uid(), "", None

    def input_state(self, active):
        if active == self.inputs.speaking:
            return
        if active:
            self.finish_caption()
        self.inputs.update(active)
        self.emit("input.state", {"state": "speaking" if active else "quiet"})
        if active:
            self.emit("transcript.delta", {"item_id": self.inputs.latest.id, "text": ""})

    def receive(self, event):
        try:
            kind = event["type"]
            if kind == "session.started":
                body = event["session"]
                if (self.ready.is_set() or body.get("status") != "active" or body.get("store") is not False
                    or body.get("audio", {}).get("format") != {"type": "audio/pcm", "rate": 24000}
                    or body.get("delegation", {}).get("type") != "client"):
                    raise ValueError("unexpected session")
                self.upstream_id = identifier(body.get("id"))
                self.ready.set()
            elif kind == "session.output_audio.delta" and not self.first_audio:
                self.first_audio = True
                logger.info("sophon_room_first_audio_frame conversation_id=%s elapsed_ms=%s",
                            self.cid, round((time.monotonic() - self.started) * 1000))
            elif kind == "session.delegation.created":
                body = event["delegation"]
                call_id = identifier(body.get("id"))
                task = body.get("task")
                if body.get("type") != "delegation" or body.get("target") != "client":
                    raise ValueError("unexpected target")
                if call_id not in self.raw_tasks and len(self.raw_tasks) >= 256:
                    raise ValueError("delegation limit")
                task = task.strip() if isinstance(task, str) and len(task) <= 2050 else ""
                if call_id in self.raw_tasks and self.raw_tasks[call_id] != task:
                    raise ValueError("conflicting delegation")
                self.raw_tasks[call_id] = task
            elif kind == "session.input_transcript.delta":
                owner = self.inputs.at_delegation(time.monotonic())
                text = event.get("delta")
                if (not isinstance(text, str) or timestamp(event.get("start_ms")) >= timestamp(event.get("end_ms"))):
                    raise ValueError("invalid input transcript")
                if owner and not owner.claimed and len(owner.transcript) + len(text) <= 2000:
                    owner.transcript += text
                    self.emit("transcript.delta", {"item_id": owner.id, "text": text})
            elif kind == "session.output_transcript.delta" and isinstance(event.get("delta"), str):
                if not self.caption_text:
                    self.caption_input = self.inputs.latest
                text = event["delta"][:2000]
                if len(self.caption_text) + len(text) <= 8000:
                    self.caption_text += text
                    self.emit("speech_text.delta", {
                        "response_id": self.caption_id, "phase": "candidate",
                        "input_item_id": self.caption_input.id if self.caption_input else None, "text": text,
                    })
            elif kind == "session.commentary.appended":
                call_id = self.receipt_ids.pop(event.get("client_event_id"), None)
                if receipt := self.receipts.get(call_id):
                    receipt.set()
            elif kind == "session.closed":
                self.confirmed = bool(self.upstream_id and event["session"].get("id") == self.upstream_id)
                self.end()
            elif kind == "error":
                self.end(DomainError("VOICE_PROVIDER_ERROR", "Sophon Live reported an error.", 502, True))
        except (DomainError, KeyError, AttributeError, TypeError, ValueError):
            self.end(DomainError("VOICE_PROTOCOL_ERROR", "Invalid Sophon Live event.", 502))

    def transcript_finished(self, event):
        owner = self.inputs.at_delegation(time.monotonic())
        if event.is_final and owner and owner.transcript and event.transcript == owner.transcript:
            self.emit("transcript.done", {"item_id": owner.id, "text": owner.transcript})

    def sent(self, event):
        kind = event.get("type")
        if kind == "session.input_audio.append":
            # Observe the same PCM the SDK is sending. SFU speaker notifications
            # can lag a short input; this observation never controls playout.
            if voiced(base64.b64decode(event["audio"]), 420):
                self.last_voiced = time.monotonic()
                self.input_state(True)
            elif self.inputs.speaking and time.monotonic() - self.last_voiced >= .32:
                self.input_state(False)
        elif kind == "session.commentary.append" and event.get("delegation_id") in self.receipts:
            self.receipt_ids[event["event_id"]] = event["delegation_id"]

    def delegate(self, event):
        if self.closed.is_set() or event.id in self.seen:
            return
        self.seen.add(event.id)
        received = time.monotonic()
        owner = self.inputs.at_delegation(received)
        try:
            self.calls.put_nowait((event, owner, received))
        except asyncio.QueueFull:
            self.end(DomainError("VOICE_TOOL_LIMIT", "Too many simultaneous voice requests.", 409))

    async def run_tools(self):
        while True:
            event, owner, received = await self.calls.get()
            task = self.raw_tasks[event.id]
            await self.current()
            if owner is None or owner is not self.inputs.latest:
                self.duplex.append_commentary(disposition("INPUT_UNCERTAIN", self.chinese), delegation_id=event.id)
                continue
            try:
                async with asyncio.timeout_at(received + 1.75):
                    while owner is self.inputs.latest and owner.ended is None:
                        self.inputs.changed.clear()
                        await self.inputs.changed.wait()
            except TimeoutError:
                pass
            if owner is not self.inputs.latest or owner.ended is None:
                self.duplex.append_commentary(disposition("INPUT_UNCERTAIN", self.chinese), delegation_id=event.id)
                continue
            transcript = owner.transcript.strip()
            decision, question = self.router.select(task, transcript, self.session.tool_catalog or {})
            # A model-derived public query must not forward knowledge excerpts
            # from earlier commentary. Use user ASR after private delivery.
            if decision and decision.tool.name == "web_search" and self.sent_private_content:
                decision, question = self.router.select("WEB_SEARCH: " + transcript if transcript else "", transcript, {})
            if owner.claimed or not decision:
                if owner.claimed:
                    content = disposition("DUPLICATE", self.chinese)
                else:
                    content = ("请向用户澄清查询对象或缺失条件。请勿声称已经查询。" if self.chinese
                               else "Ask the caller to clarify the request or missing details. No search was performed.")
                self.duplex.append_commentary(content, delegation_id=event.id)
                continue
            owner.claimed = True
            try:
                await self.execute(event.id, owner, decision, question, bool(transcript), received)
            except DomainError as exc:
                if exc.code not in {"AGENT_CAPACITY_EXCEEDED", "SERVICE_DRAINING"}:
                    raise
                self.duplex.append_commentary(disposition("FAILED", self.chinese), delegation_id=event.id)

    async def execute(self, call_id, owner, decision, question, transcribed, received):
        registry = self.coordinator.runtime.registry
        versions = {name: await self.store.tool_revision(name) for name in decision.tool.required_tools}
        if owner is not self.inputs.latest:
            return
        native_search = NativeInputSearch(1) if decision.tool.kind == "cuekb" else None
        turn, pending = await self.coordinator.submit(
            self.session.principal, self.cid, f"sophon-room:{self.epoch}:{call_id}", question, "voice",
            self.epoch, call_id, owner.id, decision, voice_relay=True, native_search=native_search,
            delegated_text=not transcribed,
        )
        self.session.request_revision = turn.request_revision
        if transcribed:
            self.emit("transcript.done", {"item_id": owner.id, "text": question})

        result = await pending if pending else None
        receipt = asyncio.Event()
        async with self.coordinator.lock(self.cid):
            await self.current()
            if not await self.store.current(self.cid, self.epoch, turn.id, turn.request_revision):
                return
            if (not decision.tool.required_tools <= await registry.allowed(self.session.principal)
                or any([await self.store.tool_revision(name) != rev for name, rev in versions.items()])
                or (native_search and await self.coordinator.runtime.catalog.revision() != self.session.catalog_revision)):
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.FORBIDDEN)
                result = None
            content = self.result_content(result)
            if owner is not self.inputs.latest or self.inputs.speaking:
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.SUPERSEDED)
                self.duplex.append_commentary(disposition("SUPERSEDED_AUDIO", self.chinese), delegation_id=call_id)
                return
            if content is None:
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.BUDGET)
                content = disposition("EVIDENCE_BUDGET", self.chinese)
            self.receipts[call_id] = receipt
            if not await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "write_started",
                                             turn.id, turn.request_revision, input_item_id=owner.id):
                self.receipts.pop(call_id, None)
                return
            if owner is not self.inputs.latest or self.inputs.speaking:
                self.receipts.pop(call_id, None)
                await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "discarded",
                                          turn.id, turn.request_revision)
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.SUPERSEDED)
                self.duplex.append_commentary(disposition("SUPERSEDED_AUDIO", self.chinese), delegation_id=call_id)
                return
            if isinstance(result, EvidenceReady):
                self.sent_private_content = True
            self.duplex.append_commentary(content, delegation_id=call_id)

        outcome = SophonEvidenceOutcome.RECEIPT_UNKNOWN
        try:
            await asyncio.wait_for(receipt.wait(), timeout=3)
            outcome = SophonEvidenceOutcome.GATEWAY_RECEIVED
            await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "sent",
                                      turn.id, turn.request_revision)
        except TimeoutError:
            pass
        finally:
            self.receipts.pop(call_id, None)
            self.receipt_ids = {eid: did for eid, did in self.receipt_ids.items() if did != call_id}
            if owner is not self.inputs.latest:
                outcome = SophonEvidenceOutcome.SUPERSEDED
            self.coordinator.sophon_evidence_outcome(turn.id, outcome)

        logger.info("sophon_room_delegation conversation_id=%s delegation_id=%s tool=%s outcome=%s elapsed_ms=%s",
                    self.cid, call_id, decision.tool.name, outcome, round((time.monotonic() - received) * 1000))

    def result_content(self, result):
        if isinstance(result, EvidenceReady):
            return commentary(result, self.chinese, self.settings.qa_direct_evidence_max_bytes)
        if result is None:
            return disposition("FAILED", self.chinese)
        payload = {"status": result.status, "search": "finished", "content": result.display_text,
                   "sources": [{"title": cite.title, "url": cite.source_uri, "excerpt": cite.content}
                               for cite in result.citations]}
        while True:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(content.encode()) <= self.settings.qa_direct_evidence_max_bytes:
                return content
            if not payload["sources"]:
                return None
            payload["sources"].pop()
            payload["status"] = "partial"

    async def write_events(self):
        while True:
            event = await self.outgoing.get()
            await self.current()
            portal_server_event_adapter.validate_python(event)
            async with asyncio.timeout(2):
                await self.ws.send_json(event)

    async def browser(self):
        while True:
            raw = await self.ws.receive_text()
            if len(raw) > 4096:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid room control event.", 400)
            try:
                event = portal_client_event_adapter.validate_json(raw)
            except ValueError as exc:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid room control event.", 400) from exc
            if event.epoch != self.epoch:
                continue
            if event.type in {"portal.session.close", "portal.interrupt"}:
                return
            if event.type != "portal.playback.stop":
                raise DomainError("VOICE_PROTOCOL_ERROR", "Use the LiveKit room for audio.", 400)

    async def watchdog(self):
        while True:
            await asyncio.sleep(.5)
            await self.current()
            if await self.coordinator.runtime.catalog.revision() != self.session.catalog_revision:
                raise DomainError("VOICE_TOOL_CONFIGURATION_CHANGED", "Voice tools changed. Start a new call.", 409)
            if time.monotonic() - self.started >= self.settings.voice_session_max_seconds:
                raise DomainError("VOICE_SESSION_EXPIRED", "Voice connection reached its time limit. Start a new call.", 409)

    async def start(self, agent_session):
        @self.room.on("participant_disconnected")
        def participant_disconnected(participant):
            if participant.identity == CALLER:
                self.end()

        self.room.on("disconnected", lambda *_: self.end())
        agent_session.on("close", lambda *_: self.end())
        agent_session.on("error", lambda *_: self.end(DomainError(
            "VOICE_PROVIDER_ERROR", "Unable to establish the Sophon Live session.", 502, True,
        )))

        @agent_session.on("agent_state_changed")
        def state_changed(state):
            if state.old_state == "speaking":
                self.finish_caption()

        instructions = (ROOT / "config" / f"sophon-livekit-prompt-{'zh' if self.chinese else 'en'}.txt").read_text()
        async with asyncio.timeout(15):
            await self.current()
            await self.room.connect(self.settings.livekit_url, room_token(self.settings, self.session.id, AGENT))
            if CALLER not in self.room.remote_participants:
                raise DomainError("VOICE_UNAVAILABLE", "The caller did not join the audio room.", 503, True)
            await agent_session.start(agent=RoomAgent(self, instructions),
                room=self.room, room_options=room_io.RoomOptions(participant_identity=CALLER),
                session_host=False, record=False)
            ready, closed = asyncio.create_task(self.ready.wait()), asyncio.create_task(self.closed.wait())
            try:
                await asyncio.wait((ready, closed), return_when=asyncio.FIRST_COMPLETED)
                if self.closed.is_set():
                    raise self.error or DomainError("VOICE_PROVIDER_ERROR", "Sophon ended the session.", 502, True)
            finally:
                ready.cancel()
                closed.cancel()
                await asyncio.gather(ready, closed, return_exceptions=True)
        self.emit("session.ready", {"sample_rate": 24000, "format": "pcm16", "chunk_ms": 80,
                                    "is_mock": self.settings.mock})


async def run_room(gateway, ws, session):
    call = RoomCall(gateway, ws, session)
    tasks = []
    try:
        async with sophon_gpt_live(call.settings.sophon_live_ws_url,
                                   call.settings.sophon_live_api_key.get_secret_value()) as model:
            agent = AgentSession(llm=model, aec_warmup_duration=None)
            try:
                await call.start(agent)
                tasks = [asyncio.create_task(fn()) for fn in
                         (call.browser, call.write_events, call.run_tools, call.watchdog, call.closed.wait)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                session.closing = True
                with anyio.CancelScope(shield=True):
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await agent.aclose()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except DomainError as exc:
        call.end(exc)
    except Exception as exc:
        logger.warning("livekit_room_closed conversation_id=%s error_type=%s", call.cid, type(exc).__name__)
        call.end(DomainError("VOICE_UNAVAILABLE", "The LiveKit voice call ended. Start a new call.", 503, True))
    finally:
        session.closing = True
        with anyio.CancelScope(shield=True):
            await call.room.disconnect()
            if call.error:
                try:
                    async with asyncio.timeout(1):
                        await ws.send_json(call.event("error", call.error.payload()))
                except Exception:
                    pass
            try:
                await call.store.settle_deliveries(call.cid, call.epoch, "connection_closed")
                await call.store.finish_voice_session(call.cid, call.epoch, session.id, call.confirmed)
                await call.coordinator.interrupt(session.principal, call.cid, call.epoch, "connection_closed")
            except DomainError:
                pass
            finally:
                gateway.sessions.pop(session.id, None)
                try:
                    await ws.close()
                except Exception:
                    pass
