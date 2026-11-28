"""Use one official LiveKit AgentSession per authorized Sophon call."""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from functools import partial

import anyio
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_input import NativeInputSearch
from app.contracts import (
    DomainError,
    PortalEvent,
    SophonDelegationDiagnostic,
    SophonDeliveryDiagnostic,
    portal_client_event_adapter,
    portal_server_event_adapter,
    uid,
)
from app.sessions.coordinator import SophonEvidenceOutcome, evidence_outcome
from app.voice.delegation_router import DelegationRouter
from app.voice.profiles import default_profile, instructions
from app.voice.sophon_diagnostics import MAX_RAW_TASK, SophonDiagnostics, delegation_preview, visible_text
from app.voice.sophon_gpt_live import sophon_gpt_live
from app.voice.sophon_live import identifier
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
    settled_at: float | None = None


@dataclass
class RawDelegation:
    task: str
    observed: SophonDelegationDiagnostic
    task_shape: tuple


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
            if (prior and prior.ended is not None and now - prior.ended <= SPEECH_PAUSE
                and not prior.claimed and prior.settled_at is None):
                prior.ended = None
            else:
                self.recent.append(SpeechInput(uid(), now))
        elif self.latest:
            self.latest.ended = now
        self.changed.set()

    def at_delegation(self, received, *, exclude_settled=True):
        candidates = self.candidates(received, exclude_settled=exclude_settled)
        return candidates[0] if len(candidates) == 1 else None

    def candidates(self, received, *, exclude_settled=True):
        return [item for item in self.recent if item.started <= received
                and (item.ended is None or received - item.ended <= INPUT_LOOKBACK)
                and not (exclude_settled and item is not self.latest and item.settled_at is not None
                         and item.settled_at <= self.latest.started)]


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
        self.diagnostics = SophonDiagnostics(self.store, self.session)
        self.diagnostics_failed = False
        self.normal_end = False
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
        self.unbound_caption_id = uid()
        self.unbound_caption_chars = 0
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
            self.unbound_caption_id, self.unbound_caption_chars = uid(), 0
        self.inputs.update(active)
        self.emit("input.state", {"state": "speaking" if active else "quiet"})
        if active:
            owner = self.inputs.latest
            self.diagnostics.input_started_at(owner.id, owner.started)
            self.diagnostics.input_resumed(owner.id, time.monotonic())
            self.emit("transcript.delta", {"item_id": owner.id, "text": ""})

    def receive(self, event):
        kind = None
        try:
            kind = event["type"]
            if not isinstance(kind, str):
                raise ValueError("invalid event type")
            if kind == "session.started":
                body = event["session"]
                if (self.ready.is_set() or body.get("status") != "active" or body.get("store") is not False
                    or body.get("audio", {}).get("format") != {"type": "audio/pcm", "rate": 24000}
                    or body.get("delegation", {}).get("type") != "client"):
                    raise ValueError("unexpected session")
                self.upstream_id = identifier(body.get("id"))
                self.ready.set()
            elif kind == "session.output_audio.delta":
                if not self.first_audio:
                    self.first_audio = True
                    logger.info("sophon_room_first_audio_frame conversation_id=%s elapsed_ms=%s",
                                self.cid, round((time.monotonic() - self.started) * 1000))
                audio = event.get("delta")
                if isinstance(audio, str) and 0 < len(audio) <= 32768:
                    try:
                        pcm = base64.b64decode(audio, validate=True)
                    except ValueError:
                        pcm = b""
                    if len(pcm) % 2 == 0 and voiced(pcm, 420):
                        self.observe_output()
            elif kind == "session.delegation.created":
                self.receive_delegation(event)
            elif kind == "session.input_transcript.delta":
                owner = self.inputs.at_delegation(time.monotonic(), exclude_settled=False)
                text = event.get("delta")
                # ASR is optional presentation data. Missing/empty/malformed
                # captions must not close the native audio/delegation path.
                if not isinstance(text, str) or not text or len(text) > 2000:
                    return
                logger.info("sophon_room_input_transcript conversation_id=%s chars=%s", self.cid, len(text))
                if owner and len(owner.transcript) + len(text) <= 2000:
                    owner.transcript += text
                    self.emit("transcript.delta", {"item_id": owner.id, "text": text})
                elif not owner and self.unbound_caption_chars + len(text) <= 2000:
                    self.unbound_caption_chars += len(text)
                    self.emit("transcript.delta", {
                        "item_id": self.unbound_caption_id, "text": text, "association": "unbound",
                    })
            elif kind == "session.output_transcript.delta" and isinstance(event.get("delta"), str):
                if event["delta"].strip():
                    self.observe_output()
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
                    observed = self.raw_tasks[call_id].observed
                    logger.info("sophon_room_commentary_ack conversation_id=%s delegation_id=%s",
                                self.cid, observed.delegation_id or observed.interaction_id)
            elif kind == "session.closed":
                self.confirmed = bool(self.upstream_id and event["session"].get("id") == self.upstream_id)
                if not self.session.closing:
                    self.normal_end = self.confirmed
                self.end()
            elif kind == "error":
                self.end(DomainError("VOICE_PROVIDER_ERROR", "Sophon Live reported an error.", 502, True))
        except (DomainError, KeyError, AttributeError, TypeError, ValueError):
            if kind != "session.delegation.created":
                self.diagnostics.note("protocol_error", "VOICE_PROTOCOL_ERROR")
            self.end(DomainError("VOICE_PROTOCOL_ERROR", "Invalid Sophon Live event.", 502))

    def receive_delegation(self, event):
        received = time.monotonic()
        observed = delegation_preview(event, self.session.id, private_context=self.sent_private_content)
        candidates = self.inputs.candidates(received)
        body = event.get("delegation")
        try:
            call_id = identifier(body.get("id") if isinstance(body, dict) else None)
            if body.get("type") != "delegation" or body.get("target") != "client":
                raise ValueError("unexpected delegation target")
        except (DomainError, ValueError):
            self.diagnostics.receive(observed, received, candidates)
            self.finish_delegation(observed, "VOICE_PROTOCOL_ERROR")
            raise
        raw_task = body.get("task")
        bounded = isinstance(raw_task, str) and len(raw_task) <= MAX_RAW_TASK
        task = raw_task.strip() if bounded else ""
        # Compare a bounded original string and type/length for opaque values.
        # Execution still deduplicates on the existing normalized task.
        shape_value = raw_task if bounded else None
        if not bounded and isinstance(raw_task, (str, list, dict)):
            shape_value = len(raw_task)
        task_shape = ("task" in body, type(raw_task), shape_value)
        prior = self.raw_tasks.get(call_id)
        if prior and prior.task == task:
            if prior.task_shape == task_shape:
                prior.observed.duplicates += 1
                self.diagnostics.publish(prior.observed, "duplicate_native_event")
            else:
                self.diagnostics.receive(observed, received, candidates)
                self.finish_delegation(observed, "CONFLICTING_DELEGATION_ID")
            return
        if prior or len(self.raw_tasks) >= 256:
            observed.structure = "invalid"
            self.diagnostics.receive(observed, received, candidates)
            self.finish_delegation(observed, "CONFLICTING_DELEGATION_ID" if prior else "VOICE_TOOL_LIMIT")
            raise ValueError("invalid delegation sequence")
        self.diagnostics.receive(observed, received, candidates)
        self.raw_tasks[call_id] = RawDelegation(task, observed, task_shape)
        route = task.partition(":")[0].strip().upper()
        logger.info("sophon_room_delegation_received conversation_id=%s delegation_id=%s route=%s",
                    self.cid, observed.delegation_id or observed.interaction_id,
                    route if route in {"WEATHER", "CUEKB", "WEB_SEARCH"} else "unclassified")

    def observe_output(self):
        at = time.monotonic()
        self.diagnostics.observe_output(at, self.inputs.candidates(at, exclude_settled=False))

    def advance_observations(self):
        at = time.monotonic()
        self.diagnostics.end_output(at)
        reply = self.diagnostics.reply
        for owner in self.inputs.recent:
            if (owner.ended is not None and at - owner.ended > INPUT_LOOKBACK
                and (not reply or owner.id not in reply.input_ids)):
                self.diagnostics.finish_input(owner.id)

    async def flush_diagnostics(self):
        try:
            async with asyncio.timeout(2):
                return await self.diagnostics.flush()
        except TimeoutError:
            logger.warning("sophon_diagnostics_incomplete conversation_id=%s epoch=%s error_type=TimeoutError",
                           self.cid, self.epoch)
            return False

    async def write_diagnostics(self):
        while True:
            try:
                await asyncio.wait_for(self.diagnostics.changed.wait(), timeout=.25)
            except TimeoutError:
                pass
            self.diagnostics.changed.clear()
            self.advance_observations()
            if await self.flush_diagnostics():
                self.diagnostics_failed = False
            else:
                if not self.diagnostics_failed and not self.outgoing.full():
                    notice = self.diagnostics.note("diagnostics_incomplete", "DIAGNOSTIC_WRITE_FAILED", persist=False)
                    self.emit("sophon_interaction.updated", notice.model_dump(mode="json"))
                self.diagnostics_failed = True
                await asyncio.sleep(.25)

    async def finish_diagnostics(self):
        at = time.monotonic()
        self.advance_observations()
        normal_end = self.normal_end and self.error is None
        close_reason = self.error.code if self.error else None
        self.diagnostics.end_output(at, closing=True, interrupted=not normal_end)
        recent = {owner.id: owner for owner in self.inputs.recent}
        for item_id in self.diagnostics.inputs:
            owner = recent.get(item_id)
            self.diagnostics.finish_input(item_id, interrupted=not normal_end or not owner or owner.ended is None,
                                          reason_code=close_reason)
        undispatched = {task.observed.interaction_id for call_id, task in self.raw_tasks.items()
                        if call_id not in self.seen}
        for item in self.diagnostics.delegations.values():
            if item.status in {"received", "processing"}:
                reason = ("SDK_DELEGATION_NOT_DISPATCHED" if item.interaction_id in undispatched
                          else close_reason or "OBSERVATION_INTERRUPTED")
                self.finish_delegation(item, reason, interrupted=True)
        await self.flush_diagnostics()

    def transcript_finished(self, event):
        owner = self.inputs.at_delegation(time.monotonic(), exclude_settled=False)
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
        if event.id not in self.raw_tasks:
            item = SophonDelegationDiagnostic(
                interaction_id=uid(), voice_session_id=self.session.id, structure="unavailable",
                delegation_id=event.id if isinstance(event.id, str) and len(event.id) <= 128
                and visible_text(event.id) else None,
            )
            self.diagnostics.receive(item, received, [owner] if owner else [])
            self.finish_delegation(item, "SDK_DELEGATION_WITHOUT_RAW_EVENT")
            self.end(DomainError("VOICE_PROTOCOL_ERROR", "Invalid Sophon Live event.", 502))
            return
        observed = self.raw_tasks[event.id].observed
        if owner is not None and observed.input_item_id is None:
            self.diagnostics.bind(observed, owner)
            self.diagnostics.publish(observed, "input_bound")
        try:
            self.calls.put_nowait((event.id, owner, received))
        except asyncio.QueueFull:
            self.finish_delegation(observed, "VOICE_TOOL_LIMIT")
            self.end(DomainError("VOICE_TOOL_LIMIT", "Too many simultaneous voice requests.", 409))

    async def run_tools(self):
        while True:
            call_id, owner, received = await self.calls.get()
            raw = self.raw_tasks[call_id]
            observed = raw.observed
            observed.status = "processing"
            self.diagnostics.publish(observed, "delegation_processing")
            try:
                await self.handle_delegation(call_id, raw.task, owner, received, observed)
            except asyncio.CancelledError:
                self.finish_delegation(observed, "OBSERVATION_INTERRUPTED", interrupted=True)
                raise
            except DomainError as exc:
                self.finish_delegation(observed, exc.code)
                raise
            except Exception:
                self.finish_delegation(observed, "VOICE_UNAVAILABLE")
                raise
            else:
                self.finish_delegation(observed)

    async def handle_delegation(self, call_id, task, owner, received, observed):
        await self.current()
        if owner is None or owner is not self.inputs.latest:
            await self.respond(call_id, None, observed, "INPUT_UNCERTAIN")
            return
        try:
            async with asyncio.timeout_at(received + 1.75):
                while owner is self.inputs.latest and owner.ended is None:
                    self.inputs.changed.clear()
                    await self.inputs.changed.wait()
        except TimeoutError:
            pass
        if owner is not self.inputs.latest or owner.ended is None:
            await self.respond(call_id, None, observed, "INPUT_UNCERTAIN")
            return
        if owner.claimed:
            await self.respond(call_id, owner, observed, "DUPLICATE")
            return
        input_ended = owner.ended
        parsed = await self.router.parse_native(task, self.session.tool_catalog or {},
            private_context=self.sent_private_content)
        self.diagnostics.route(observed, parsed, self.settings.delegation_router_model,
                               private_context=self.sent_private_content)
        await self.current()
        if owner is not self.inputs.latest or owner.ended != input_ended or self.inputs.speaking:
            await self.respond(call_id, owner, observed, "INPUT_UNCERTAIN")
            return
        decision, question = parsed.decision, parsed.question
        logger.info("sophon_room_route conversation_id=%s delegation_id=%s route=%s parse_method=%s tool=%s reason=%s elapsed_ms=%s",
                    self.cid, observed.delegation_id or observed.interaction_id, parsed.route, parsed.parse_method,
                    decision.tool.name if decision else "none", parsed.reason or "ok",
                    round((time.monotonic() - received) * 1000))
        visible_task = observed.native_event["delegation"].get("task")
        await self.store.record(self.cid, self.epoch, "sophon_route", observed.delegation_id or observed.interaction_id, {
            "delegation_task": task if isinstance(visible_task, str) else None,
            "route": parsed.route, "parse_method": parsed.parse_method,
            "source": parsed.source, "reason": parsed.reason,
            "router_model": observed.intent.router_model,
            "_authorized_kb_ids": sorted(self.session.principal.knowledge_base_ids),
        })
        if owner is not self.inputs.latest or owner.ended != input_ended or self.inputs.speaking:
            await self.respond(call_id, owner, observed, "INPUT_UNCERTAIN")
            return
        if owner.claimed:
            await self.respond(call_id, owner, observed, "DUPLICATE")
            return
        owner.claimed = True
        if not decision:
            if parsed.reason.startswith("llm_"):
                content = ("意图解析暂不可用或结果不可靠，未执行查询。请向用户说明并请其澄清或重试。" if self.chinese
                           else "Intent parsing is unavailable or unreliable. No lookup was performed. Ask the caller to clarify or retry.")
            elif parsed.route == "UNSUPPORTED":
                content = ("此功能尚未接入，未执行任何操作，请明确告诉用户。" if self.chinese
                           else "This operation is unsupported. Tell the caller no action was performed.")
            else:
                content = ("请向用户澄清查询对象、公开资料范围或缺失条件。请勿声称已经查询。" if self.chinese
                           else "Clarify the target, public information scope or missing details. No search was performed.")
            await self.respond(call_id, owner, observed, parsed.reason or parsed.route, content)
            return
        if decision.tool.name == "web_search" and self.sent_private_content:
            await self.respond(call_id, owner, observed, "PRIVATE_WEB_SEARCH")
            return
        try:
            await self.execute(call_id, owner, decision, question, received, observed)
        except DomainError as exc:
            if exc.code not in {"AGENT_CAPACITY_EXCEEDED", "SERVICE_DRAINING"}:
                raise
            await self.respond(call_id, owner, observed, exc.code, disposition("FAILED", self.chinese))

    def record_delivery(self, item, kind, status, reason_code=None):
        item.delivery = SophonDeliveryDiagnostic(kind=kind, status=status, reason_code=reason_code)
        self.diagnostics.publish(item, f"delivery_{status}")

    def finish_delegation(self, item, reason_code=None, *, interrupted=False):
        item.reason_code = reason_code or item.reason_code
        if item.delivery and item.delivery.status == "queued":
            item.delivery.status = "receipt_unknown"
        item.status = "interrupted" if interrupted else "finished"
        self.diagnostics.publish(item, "delegation_finished")

    async def respond(self, call_id, owner, observed, reason_code, content=None):
        observed.reason_code = reason_code
        receipt = asyncio.Event()
        self.receipts[call_id] = receipt
        self.duplex.append_commentary(content or disposition(reason_code, self.chinese), delegation_id=call_id)
        self.record_delivery(observed, "disposition", "queued", reason_code)
        ack = await self.wait_receipt(call_id, receipt, owner)
        self.record_delivery(observed, "disposition", "gateway_received" if ack else "receipt_unknown", reason_code)

    async def wait_receipt(self, call_id, receipt, owner):
        try:
            await asyncio.wait_for(receipt.wait(), timeout=3)
            # Retire only before later speech begins; a receipt is not spoken-
            # answer verification and cannot disambiguate an overlapping input.
            if owner is not None and owner is self.inputs.latest and not self.inputs.speaking:
                owner.settled_at = time.monotonic()
                self.inputs.changed.set()
            return True
        except TimeoutError:
            return False
        finally:
            self.receipts.pop(call_id, None)
            self.receipt_ids = {eid: did for eid, did in self.receipt_ids.items() if did != call_id}

    async def execute(self, call_id, owner, decision, question, received, observed):
        registry = self.coordinator.runtime.registry
        versions = {name: await self.store.tool_revision(name) for name in decision.tool.required_tools}
        if owner is not self.inputs.latest:
            observed.reason_code = "INPUT_UNCERTAIN"
            return
        native_search = NativeInputSearch(1) if decision.tool.kind == "cuekb" else None
        turn, pending = await self.coordinator.submit(
            self.session.principal, self.cid, f"sophon-room:{self.epoch}:{call_id}", question, "voice",
            self.epoch, call_id, owner.id, decision, voice_relay=True, native_search=native_search,
            delegated_text=True, tool_observer=partial(self.diagnostics.tool, observed),
        )
        self.session.request_revision = turn.request_revision
        observed.turn_id, observed.request_revision = turn.id, turn.request_revision
        self.diagnostics.publish(observed, "turn_submitted")

        result = await pending if pending else None
        if isinstance(result, EvidenceReady):
            observed.result_status, observed.result_reason = evidence_outcome(result.envelope)
        elif result is not None:
            observed.result_status, observed.result_reason = result.status, result.reason_code
        else:
            observed.result_reason = "NO_CURRENT_RESULT"
        self.diagnostics.publish(observed, "execution_result")
        receipt = asyncio.Event()
        async with self.coordinator.lock(self.cid):
            await self.current()
            if not await self.store.current(self.cid, self.epoch, turn.id, turn.request_revision):
                self.record_delivery(observed, "tool_result", "discarded", "STALE_TURN")
                return
            delivery_reason = None
            if (not decision.tool.required_tools <= await registry.allowed(self.session.principal)
                or any([await self.store.tool_revision(name) != rev for name, rev in versions.items()])
                or (native_search and await self.coordinator.runtime.catalog.revision() != self.session.catalog_revision)):
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.FORBIDDEN)
                result = None
                delivery_reason = "FORBIDDEN"
            content = self.result_content(result)
            if owner is not self.inputs.latest or self.inputs.speaking:
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.SUPERSEDED)
                self.record_delivery(observed, "tool_result", "discarded", "SUPERSEDED_AUDIO")
                self.duplex.append_commentary(disposition("SUPERSEDED_AUDIO", self.chinese), delegation_id=call_id)
                return
            if content is None:
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.BUDGET)
                content = disposition("EVIDENCE_BUDGET", self.chinese)
                delivery_reason = "EVIDENCE_BUDGET"
            delivery_kind = "tool_result" if result is not None and not delivery_reason else "disposition"
            self.receipts[call_id] = receipt
            if not await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "write_started",
                                             turn.id, turn.request_revision, input_item_id=owner.id):
                self.receipts.pop(call_id, None)
                self.record_delivery(observed, delivery_kind, "discarded", "STALE_TURN")
                return
            if owner is not self.inputs.latest or self.inputs.speaking:
                self.receipts.pop(call_id, None)
                await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "discarded",
                                          turn.id, turn.request_revision)
                self.coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.SUPERSEDED)
                self.record_delivery(observed, delivery_kind, "discarded", "SUPERSEDED_AUDIO")
                self.duplex.append_commentary(disposition("SUPERSEDED_AUDIO", self.chinese), delegation_id=call_id)
                return
            if isinstance(result, EvidenceReady):
                self.sent_private_content = True
            self.duplex.append_commentary(content, delegation_id=call_id)
            self.record_delivery(observed, delivery_kind, "queued", delivery_reason)

        outcome = SophonEvidenceOutcome.RECEIPT_UNKNOWN
        try:
            if await self.wait_receipt(call_id, receipt, owner):
                outcome = SophonEvidenceOutcome.GATEWAY_RECEIVED
                self.record_delivery(observed, delivery_kind, "gateway_received", delivery_reason)
                await self.store.delivery(self.cid, self.epoch, call_id, "tool_result", "sent",
                                          turn.id, turn.request_revision)
            else:
                self.record_delivery(observed, delivery_kind, "receipt_unknown", delivery_reason)
        finally:
            if owner is not self.inputs.latest:
                outcome = SophonEvidenceOutcome.SUPERSEDED
            self.coordinator.sophon_evidence_outcome(turn.id, outcome)

        logger.info("sophon_room_delegation conversation_id=%s delegation_id=%s tool=%s outcome=%s elapsed_ms=%s",
                    self.cid, observed.delegation_id or observed.interaction_id, decision.tool.name, outcome,
                    round((time.monotonic() - received) * 1000))

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
                self.normal_end = event.type == "portal.session.close"
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

        prompt = self.session.instructions or instructions(default_profile(), self.settings.sophon_live_locale, livekit=True)
        async with asyncio.timeout(15):
            await self.current()
            await self.room.connect(self.settings.livekit_url, room_token(self.settings, self.session.id, AGENT))
            if CALLER not in self.room.remote_participants:
                raise DomainError("VOICE_UNAVAILABLE", "The caller did not join the audio room.", 503, True)
            await agent_session.start(agent=RoomAgent(self, prompt),
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
                tasks = [asyncio.create_task(fn()) for fn in (
                    call.browser, call.write_events, call.run_tools, call.watchdog,
                    call.write_diagnostics, call.closed.wait,
                )]
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
            await call.finish_diagnostics()
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
