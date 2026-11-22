"""Relay continuous Live audio and bind client delegations to observed audio."""

import asyncio
import base64
import json
import logging
import struct
import time
from dataclasses import dataclass

import anyio
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.dispatch import ExecutionDecision
from app.agent_runtime.native_input import NativeInputSearch
from app.contracts import (
    AnswerBundle,
    DomainError,
    PortalAudioAppend,
    PortalEvent,
    PortalPlaybackAck,
    PortalPlaybackStop,
    PortalSessionClose,
    StrictModel,
    portal_client_event_adapter,
    portal_server_event_adapter,
    printable_ascii,
    uid,
)
from app.sessions.coordinator import SophonEvidenceOutcome
from app.voice.sophon_live import RATE
from pydantic import Field
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)
ASR_SETTLE = 0.25
INPUT_GAP_MS = 800
OUTPUT_GAP_MS = 900
DELEGATION_INPUT_LOOKBACK_MS = 8000
TOOL = "search_knowledge_base"


def voiced(pcm, threshold):
    # Count RMS in short windows so low-energy speech on a frame boundary
    # does not disappear into the average of an entire audio packet.
    for offset in range(0, len(pcm), 960):
        window = pcm[offset:offset + 960]
        if sum(value * value for (value,) in struct.iter_unpack("<h", window)) > threshold**2 * (len(window) // 2):
            return True
    return False


@dataclass
class InputTurn:
    id: str
    start: int
    end: int
    text: str = ""
    asr_start: int = -1
    asr_end: int = -1
    asr_at: float = 0
    caption_done: bool = False
    delegated: bool = False
    settled_without_evidence: bool = False
    delegations: int = 0


class InputTurnAssembler:
    def __init__(self):
        self.turns = []

    @property
    def latest(self):
        return self.turns[-1] if self.turns else None

    def speech(self, clock):
        turn = self.latest
        if not turn or turn.delegations or clock - turn.end >= INPUT_GAP_MS:
            if len(self.turns) >= 512:
                raise DomainError("VOICE_SESSION_ROTATION_REQUIRED", "Reconnect to continue voice.", 409, True)
            turn = InputTurn(uid(), clock, clock)
            self.turns.append(turn)
        turn.end = max(turn.end, clock + 80)
        return turn

    def transcript(self, text, start, end):
        candidates = [turn for turn in self.turns if
                      start < turn.end + 200 and end > turn.start - 200]
        # Prefer real overlap over timestamp jitter for display attribution.
        overlaps = [turn for turn in candidates if start < turn.end and end > turn.start]
        candidates = overlaps or candidates
        if len(candidates) != 1:
            return None
        turn = candidates[0]
        if start == turn.asr_end or (not turn.text and turn.asr_end < 0):
            if turn.caption_done or len(turn.text) + len(text) > 2000:
                return None
            if not turn.text:
                turn.asr_start = start
            turn.asr_end = end
            turn.text += text
            turn.asr_at = time.monotonic()
            return turn
        return None

    def at_delegation(self, offset):
        # Live stamps the event after waiting on ASR. Its timestamp can fall
        # inside a later utterance. Even an input with no ASR yet competes.
        candidates = [turn for turn in self.turns
                      if turn.start <= offset and offset - turn.end <= DELEGATION_INPUT_LOOKBACK_MS]
        return candidates[0] if len(candidates) == 1 and candidates[0] is self.latest else None

    @staticmethod
    def audio_settled(turn, clock):
        return clock - turn.end >= 320


@dataclass
class Delegation:
    id: str
    task: str
    offset: int
    received: float
    owner: InputTurn | None


@dataclass
class ResultDelivery:
    call: Delegation
    turn: object
    sent_at: float
    ack_offset: int | None = None


@dataclass
class Segment:
    id: str
    start: int
    last_voice: int
    owner: InputTurn | None
    delivery: ResultDelivery | None = None
    text: str = ""
    sent_samples: int = 0
    acknowledged: int = 0
    sent_done: bool = False
    playback_started: bool = False
    first_audio_at: float = 0


async def sophon_relay(gateway, ws, session, provider):
    store, coordinator, settings = gateway.store, gateway.coordinator, gateway.settings
    cid, epoch = session.conversation_id, session.epoch
    inputs = InputTurnAssembler()
    outgoing, calls = asyncio.Queue(maxsize=12), asyncio.Queue(maxsize=8)
    upstream_lock = asyncio.Lock()
    input_changed = asyncio.Event()
    seen, segments = {}, {}
    clock = samples = seq = 0
    client_seq = -1
    segment = delivery = leading = None
    receipts = {}
    interrupted_delivery = False
    tasks = []
    error = None
    created_at = last_input = time.monotonic()
    speaking = False
    voiced_frames = quiet_frames = muted_until_quiet = 0
    chinese = settings.sophon_live_locale == "zh-CN"

    async def current():
        await coordinator.coordination.check(cid)
        if session.principal.expires_at is not None and session.principal.expires_at <= time.time():
            raise DomainError("AUTH_REQUIRED", "Your session expired. Please sign in again.", 401)
        if session.stopped or not await store.current(cid, epoch):
            raise DomainError("STALE_EPOCH", "This voice connection has expired.", 409)

    async def catalog_current():
        if await coordinator.runtime.catalog.revision() != session.catalog_revision:
            raise DomainError("VOICE_TOOL_CONFIGURATION_CHANGED", "Voice tools changed. Reconnect.", 409, True)

    def emit(kind, payload, turn_id=None):
        nonlocal seq
        seq += 1
        event = PortalEvent(type="portal." + kind, conversation_id=cid, epoch=epoch,
            request_revision=session.request_revision, server_seq=seq, payload=payload, turn_id=turn_id)
        data = event.model_dump(mode="json")
        portal_server_event_adapter.validate_python(data)
        try:
            outgoing.put_nowait(data)
        except asyncio.QueueFull as exc:
            raise DomainError("AUDIO_BACKPRESSURE", "Playback fell behind. Restart voice.", 409, True) from exc

    async def finalize_caption(turn):
        turn.caption_done = True
        await store.record(cid, epoch, "sophon_input", turn.id, {
            "audio_start_ms": turn.start, "audio_end_ms": turn.end,
            "asr_start_ms": turn.asr_start, "asr_end_ms": turn.asr_end,
        })
        emit("transcript.done", {"item_id": turn.id, "text": turn.text.strip()})

    def response_body(response):
        owner = response.owner
        operation = response.delivery
        if operation or interrupted_delivery:
            phase = "candidate"
        else:
            phase = "status" if owner and owner.delegated and not owner.settled_without_evidence else "answer"
        return {"response_id": response.id, "input_item_id": owner.id if owner else None, "phase": phase,
                "answer_kind": "knowledge" if operation else "general"}

    async def finish_segment():
        nonlocal segment
        response, segment = segment, None
        if response.id in session.suppressed_responses:
            return
        operation = response.delivery
        turn_id = operation.turn.id if operation else None
        if response.text.strip():
            emit("speech_text.done", {**response_body(response), "text": response.text}, turn_id)
        emit("audio.done", {"response_id": response.id, "phase": response_body(response)["phase"]}, turn_id)
        if operation and response.text.strip():
            # This measures output near a receipt, not the model's consumption
            # of the result. A later segment can contain the actual answer.
            await store.record(cid, epoch, "sophon_audio_candidate", response.id, {
                "nearby_delegation_id": operation.call.id, "input_item_id": operation.call.owner.id,
                "first_audio_ms": response.start, "commentary_ack_ms": operation.ack_offset,
                "send_to_candidate_audio_ms": round((response.first_audio_at - operation.sent_at) * 1000),
                "input_to_candidate_audio_ms": response.start - operation.call.owner.end,
                "_authorized_kb_ids": sorted(session.principal.knowledge_base_ids),
            }, turn_id, operation.turn.request_revision)
        elif (not interrupted_delivery and response.owner and not response.owner.delegated
              and response.start >= response.owner.end - 100 and response.text.strip()):
            await store.utterance(session.principal, cid, epoch, response.owner.id, answer=AnswerBundle(
                status="answered", display_text=response.text.strip(), speech_text="",
                speech_language=settings.sophon_live_locale, answer_kind="general",
                composition="provider_general", validation_level="provider_only"))

    def suppress_playback(response_id=None):
        nonlocal segment, leading, muted_until_quiet
        for response in segments.values():
            if response.id == response_id or response_id is None and (
                not response.sent_done or response.acknowledged < response.sent_samples
            ):
                session.suppressed_responses.add(response.id)
        if response_id is None or segment and segment.id == response_id:
            segment = leading = None
            muted_until_quiet = 1
        emit("playback.clear", {"response_id": response_id})

    async def writer():
        while True:
            event = await outgoing.get()
            await current()
            body, kind = event["payload"], event["type"]
            rid = body.get("response_id")
            if rid in session.suppressed_responses and kind != "portal.playback.clear":
                continue
            response = segments.get(rid)
            if kind == "portal.audio.delta":
                response.sent_samples += len(base64.b64decode(body["audio"])) // 2
            elif kind == "portal.audio.done":
                response.sent_done = True
            async with asyncio.timeout(2):
                await ws.send_json(event)

    async def browser():
        nonlocal client_seq, last_input, voiced_frames, quiet_frames, speaking, delivery, interrupted_delivery
        frames = 0
        while True:
            raw = await ws.receive_text()
            if len(raw) > 10000:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Audio event exceeds the size limit.", 400)
            try:
                event = portal_client_event_adapter.validate_json(raw)
            except ValueError as exc:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid portal voice event.", 400) from exc
            await current()
            if event.epoch != epoch:
                continue
            if isinstance(event, PortalAudioAppend):
                if event.seq <= client_seq:
                    continue
                if client_seq >= 0 and event.seq != client_seq + 1:
                    raise DomainError("AUDIO_BACKPRESSURE", "Audio was interrupted. Restart voice.", 409)
                client_seq = event.seq
                frames += 1
                last_input = time.monotonic()
                if frames * .08 - (last_input - created_at) > .5:
                    raise DomainError("AUDIO_BACKPRESSURE", "Audio was sent too quickly.", 409)
                if voiced(base64.b64decode(event.payload.audio), 420):
                    owner = inputs.speech(clock)
                    input_changed.set()
                    if delivery and owner is not delivery.call.owner:
                        # Keep listening. Live cannot identify a belated tool
                        # reply, so later speech cannot enter conversation history.
                        interrupted_delivery = True
                        coordinator.sophon_evidence_outcome(delivery.turn.id, SophonEvidenceOutcome.SUPERSEDED)
                        delivery = None
                    voiced_frames += 1
                    quiet_frames = 0
                    if not speaking and voiced_frames >= 2:
                        speaking = True
                        if segment or any(r.sent_samples > r.acknowledged for r in segments.values()
                                          if r.id not in session.suppressed_responses):
                            suppress_playback()
                        emit("input.state", {"state": "speaking"})
                else:
                    voiced_frames = 0
                    quiet_frames += 1
                    if speaking and quiet_frames >= 4:
                        speaking = False
                        emit("input.state", {"state": "quiet"})
                async with upstream_lock, asyncio.timeout(2):
                    await provider.send_audio(event.payload.audio)
            elif isinstance(event, PortalPlaybackStop):
                suppress_playback(event.payload.response_id)
            elif isinstance(event, PortalPlaybackAck):
                response = segments.get(event.payload.response_id)
                if not response or event.payload.played_samples > response.sent_samples or (
                    event.payload.finished and not response.sent_done
                ):
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid playback acknowledgment.", 400)
                response.acknowledged = max(response.acknowledged, event.payload.played_samples)
                if response.acknowledged and not response.playback_started:
                    response.playback_started = True
                    logger.info("sophon_playback_observed conversation_id=%s response_id=%s post_commentary_candidate=%s",
                                cid, response.id, response.delivery is not None)
            elif isinstance(event, PortalSessionClose) or event.type == "portal.interrupt":
                return

    async def receiver():
        nonlocal clock, samples, segment, leading, muted_until_quiet
        async for event in provider.events():
            await current()
            body = event.payload
            if event.kind == "input_text":
                turn = inputs.transcript(body["text"], body["start"], body["end"])
                if turn:
                    emit("transcript.delta", {"item_id": turn.id, "text": body["text"]})
                else:
                    emit("transcript.delta", {"item_id": uid(), "text": body["text"], "association": "unbound"})
            elif event.kind == "delegation":
                did, task = body["id"], body["task"]
                if did in seen:
                    if seen[did] != task:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Sophon reused a delegation ID.", 502)
                    continue
                if len(seen) >= 128 or calls.full():
                    raise DomainError("VOICE_TOOL_LIMIT", "Too many voice delegations.", 502, True)
                seen[did] = task
                call = Delegation(did, task, body["offset"], time.monotonic(),
                                  inputs.at_delegation(body["offset"]))
                if call.owner and knowledge_task(task):
                    call.owner.delegated = True
                calls.put_nowait(call)
            elif event.kind == "ack":
                receipt = receipts.pop(body["event_id"], None)
                if receipt:
                    receipt.ack_offset = body["offset"]
                    coordinator.sophon_evidence_outcome(receipt.turn.id, SophonEvidenceOutcome.GATEWAY_RECEIVED)
                    logger.info("sophon_commentary_ack conversation_id=%s delegation_id=%s offset_ms=%s",
                                cid, receipt.call.id, receipt.ack_offset)
                    await store.record(cid, epoch, "sophon_commentary_ack", receipt.call.id, {
                        "client_event_id": body["event_id"], "offset_ms": receipt.ack_offset,
                        "input_item_id": receipt.call.owner.id, "basis": "gateway_receipt",
                        "_authorized_kb_ids": sorted(session.principal.knowledge_base_ids),
                    })
            elif event.kind == "output_text":
                # Timestamps refer to the shared output clock, not a tool ID.
                if segment and segment.start - 200 <= body["start"] <= clock + 200:
                    if len(segment.text) + len(body["text"]) > 8000:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Sophon response transcript is too long.", 502)
                    segment.text += body["text"]
                    operation = segment.delivery
                    emit("speech_text.delta", {**response_body(segment), "text": body["text"]},
                         operation.turn.id if operation else None)
            elif event.kind == "audio":
                start = clock
                samples += len(body["pcm"]) // 2
                clock = samples * 1000 // RATE
                audible = voiced(body["pcm"], 120)
                if muted_until_quiet:
                    muted_until_quiet = 1 if audible or speaking else muted_until_quiet + 1
                    if muted_until_quiet >= 3:
                        muted_until_quiet = 0
                    continue
                if not segment and not audible:
                    leading = body["audio"]
                    continue
                candidate = delivery if delivery and (
                    delivery.ack_offset is not None and start >= delivery.ack_offset + 200
                    and inputs.latest is delivery.call.owner
                    and time.monotonic() - delivery.sent_at < settings.voice_native_answer_timeout_seconds
                ) else None
                if audible and segment and not segment.delivery and candidate and start - segment.last_voice >= 200:
                    await finish_segment()
                if audible and not segment:
                    # A nearby receipt gives context for display only.
                    operation = candidate if candidate is delivery else None
                    segment = Segment(uid(), start, clock, inputs.latest, operation, first_audio_at=time.monotonic())
                    segments[segment.id] = segment
                    if operation:
                        logger.info("sophon_audio_candidate conversation_id=%s nearby_delegation_id=%s response_id=%s wait_ms=%s",
                                    cid, operation.call.id, segment.id, round((time.monotonic() - operation.sent_at) * 1000))
                    if session.suppress_next_response:
                        session.suppress_next_response = False
                        session.suppressed_responses.add(segment.id)
                    if leading:
                        emit("audio.delta", {"response_id": segment.id, "audio": leading},
                             operation.turn.id if operation else None)
                        leading = None
                if audible:
                    segment.last_voice = clock
                if clock - segment.last_voice >= OUTPUT_GAP_MS:
                    await finish_segment()
                else:
                    emit("audio.delta", {"response_id": segment.id, "audio": body["audio"]},
                         segment.delivery.turn.id if segment.delivery else None)
        if not provider.close_confirmed:
            raise DomainError("VOICE_PROVIDER_CLOSED", "Sophon Live closed the connection.", 502, True)

    async def write_result(call, content, turn=None, observe=False):
        nonlocal delivery
        replaced = None
        async with upstream_lock, asyncio.timeout(2):
            await current()
            if observe and call.owner is not inputs.latest:
                replaced = "SUPERSEDED_AUDIO"
                observe = False
                content = disposition(replaced, chinese)
                coordinator.sophon_evidence_outcome(turn.id, SophonEvidenceOutcome.SUPERSEDED)
            event_id = uid()
            sent = time.monotonic()
            if observe:
                delivery = ResultDelivery(call, turn, sent)
                receipts[event_id] = delivery
            await provider.submit_result(call.id, content, event_id=event_id)
        logger.info("sophon_commentary_sent conversation_id=%s delegation_id=%s status=%s total_ms=%s",
                    cid, call.id, "evidence_decision" if observe else "disposition", round((sent - call.received) * 1000))
        return replaced

    async def tool_worker():
        while True:
            call = await calls.get()
            await store.record(cid, epoch, "sophon_delegation", call.id, {
                "task": call.task, "offset_ms": call.offset, "input_item_id": call.owner.id if call.owner else None,
                "_authorized_kb_ids": sorted(session.principal.knowledge_base_ids),
            })
            code, turn, result, decision = None, None, None, None
            retrieval_ms = None
            versions = {}
            owner = call.owner
            tool = (session.tool_catalog or {}).get(TOOL)
            question = knowledge_task(call.task)
            if not tool or tool.kind != "cuekb" or call.task.startswith("UNSUPPORTED:"):
                code = "UNSUPPORTED"
            elif not question or not any(char.isalnum() for char in question):
                code = "ROUTE_UNDECLARED"
            elif owner is None or owner is not inputs.latest:
                code = "INPUT_UNCERTAIN"
            else:
                try:
                    async with asyncio.timeout_at(call.received + 1.25):
                        while owner is inputs.latest and not inputs.audio_settled(owner, clock):
                            input_changed.clear()
                            await input_changed.wait()
                except TimeoutError:
                    pass
                if (owner is not inputs.latest or not inputs.audio_settled(owner, clock)
                    or owner.end > call.offset + 200):
                    code = "INPUT_UNCERTAIN"
                elif owner.delegations >= settings.sophon_live_max_delegations_per_input:
                    code = "DUPLICATE"
                elif not chinese and not printable_ascii(question):
                    code = "INPUT_UNCERTAIN"
                else:
                    owner.delegations += 1
                    decision = ExecutionDecision(tool, SophonQuestion(query=question))
                    search = NativeInputSearch(1)
                    versions = {name: await store.tool_revision(name) for name in tool.required_tools}
                    await current()
                    await catalog_current()
                    retrieval_started = time.monotonic()
                    try:
                        turn, task = await coordinator.submit(
                            session.principal, cid, f"sophon:{epoch}:{call.id}", question,
                            "voice", epoch, call.id, owner.id, decision, voice_relay=True, native_search=search,
                            delegated_text=True,
                        )
                        session.request_revision = turn.request_revision
                        result = await task if task else None
                    except DomainError as exc:
                        if exc.code in {"STALE_EPOCH", "GATEWAY_LEASE_LOST", "SESSION_OWNED_BY_OTHER", "AUTH_REQUIRED"}:
                            raise
                        code = exc.code
                    else:
                        if not isinstance(result, EvidenceReady):
                            code = result.reason_code if result else "FAILED"
                            code = code or "FAILED"
                    finally:
                        retrieval_ms = round((time.monotonic() - retrieval_started) * 1000)
            async with coordinator.lock(cid):
                await current()
                await catalog_current()
                if turn and not await store.current(cid, epoch, turn.id, turn.request_revision):
                    raise DomainError("STALE_REVISION", "The tool request changed before delivery.", 409)
                if isinstance(result, EvidenceReady):
                    allowed = await coordinator.runtime.registry.allowed(session.principal)
                    if (not decision.tool.required_tools <= allowed
                        or decision.tool.permission_scope not in session.principal.scopes
                        or any([await store.tool_revision(name) != rev for name, rev in versions.items()])):
                        code = "FORBIDDEN"
                    elif owner is not inputs.latest:
                        code = "SUPERSEDED_AUDIO"
                    elif not session.suppressed_turns or turn.id not in session.suppressed_turns:
                        content = commentary(result, chinese, settings.qa_direct_evidence_max_bytes)
                        if content is None:
                            code = "EVIDENCE_BUDGET"
                    else:
                        code = "SUPERSEDED_AUDIO"
                    if code:
                        coordinator.sophon_evidence_outcome(turn.id, {
                            "FORBIDDEN": SophonEvidenceOutcome.FORBIDDEN,
                            "SUPERSEDED_AUDIO": SophonEvidenceOutcome.SUPERSEDED,
                            "EVIDENCE_BUDGET": SophonEvidenceOutcome.BUDGET,
                        }[code])
                if not code and not isinstance(result, EvidenceReady):
                    code = "FAILED"
                if code == "AGENT_TIMEOUT":
                    code = "TIMEOUT"
                if code:
                    content = disposition(code, chinese)
                    if owner and code != "DUPLICATE" and owner is inputs.latest:
                        owner.settled_without_evidence = True
                delivery_args = (turn.id, turn.request_revision) if turn else (None, 0)
                if not await store.delivery(cid, epoch, call.id, "tool_result", "write_started", *delivery_args,
                                            input_item_id=owner.id if owner else None):
                    raise DomainError("STALE_REVISION", "The tool result is no longer current.", 409)
                code = await write_result(call, content, turn, observe=code is None) or code
                await store.delivery(cid, epoch, call.id, "tool_result", "sent", *delivery_args)
            await store.record(cid, epoch, "sophon_commentary", call.id, {
                "status": code or result.envelope["decision"], "input_item_id": owner.id if owner else None,
                "delegation_ms_after_audio": call.offset - owner.end if owner else None,
                "query_and_validation_ms": retrieval_ms,
                "elapsed_ms": round((time.monotonic() - call.received) * 1000),
                "_authorized_kb_ids": sorted(session.principal.knowledge_base_ids),
            })

    async def watchdog():
        while True:
            await asyncio.sleep(.25)
            await current()
            for owner in inputs.turns:
                if (not owner.caption_done and owner.text.strip() and inputs.audio_settled(owner, clock)
                        and time.monotonic() - owner.asr_at >= ASR_SETTLE):
                    await finalize_caption(owner)
            input_changed.set()
            for event_id, receipt in list(receipts.items()):
                if time.monotonic() - receipt.sent_at >= settings.voice_native_answer_timeout_seconds:
                    receipts.pop(event_id)
                    logger.warning("sophon_commentary_receipt_timeout conversation_id=%s delegation_id=%s",
                                   cid, receipt.call.id)
                    coordinator.sophon_evidence_outcome(receipt.turn.id, SophonEvidenceOutcome.RECEIPT_UNKNOWN)
            if time.monotonic() - last_input > 5:
                raise DomainError("AUDIO_BACKPRESSURE", "Audio capture stalled. Restart voice.", 409, True)
            if time.monotonic() - created_at > settings.voice_session_max_seconds:
                if not speaking and segment is None and not receipts and calls.empty() and coordinator.tasks.get(cid) is None:
                    raise DomainError("VOICE_SESSION_ROTATION_REQUIRED", "Reconnect to continue voice.", 409, True)
                if time.monotonic() - created_at > settings.voice_session_max_seconds + 30:
                    raise DomainError("VOICE_SESSION_EXPIRED", "The voice connection reached its time limit.", 409, True)

    try:
        await current()
        await provider.connect()
        logger.info("sophon_session_started conversation_id=%s voice_session_id=%s upstream_session_id=%s",
                    cid, session.id, provider.session_id)
        created_at = last_input = time.monotonic()
        emit("session.ready", {"sample_rate": RATE, "format": "pcm16", "chunk_ms": 80, "is_mock": settings.mock})
        tasks = [asyncio.create_task(fn()) for fn in (writer, receiver, browser, tool_worker, watchdog)]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except DomainError as exc:
        error = exc
    except (asyncio.CancelledError, WebSocketDisconnect):
        pass
    except Exception as exc:
        logger.warning("sophon_relay_failed conversation_id=%s error_type=%s", cid, type(exc).__name__)
        error = DomainError("VOICE_UNAVAILABLE", "Sophon Live ended the connection. Restart voice.", 503, True)
    finally:
        session.closing = True
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if error:
                try:
                    async with asyncio.timeout(1):
                        await ws.send_json(PortalEvent(type="portal.error", conversation_id=cid, epoch=epoch,
                            request_revision=session.request_revision, server_seq=seq + 1,
                            payload=error.payload()).model_dump(mode="json"))
                except Exception:
                    pass
            confirmed = False
            try:
                async with asyncio.timeout(3):
                    confirmed = await provider.close()
            except Exception:
                pass
            finally:
                try:
                    await store.settle_deliveries(cid, epoch, "connection_closed")
                    await store.finish_voice_session(cid, epoch, session.id, confirmed)
                    await coordinator.interrupt(session.principal, cid, epoch, "connection_closed")
                except DomainError:
                    pass
                finally:
                    gateway.sessions.pop(session.id, None)
                    try:
                        await ws.close()
                    except Exception:
                        pass


def knowledge_task(task):
    # Lazy import keeps the shared rule parser and existing SophonQuestion
    # contract in their current modules without changing the WS tool scope.
    from app.voice.delegation_router import natural_task
    route, sep, question = task.replace("：", ":", 1).partition(":")
    if not sep:
        route, question = natural_task(task)
    return question.strip() if route.strip().upper() == "CUEKB" else ""


class SophonQuestion(StrictModel):
    """Question supplied by a bound read-only Sophon delegation."""

    # The session's locale decides whether Unicode is accepted. NVIDIA's tool
    # schema and validation remain unchanged.
    query: str = Field(min_length=1, max_length=2000)


def commentary(ready, chinese, max_bytes):
    envelope = ready.envelope
    if envelope.get("schema_version") != "cuekb-evidence-v2.1":
        return None
    packet = dict(envelope["model_view"])
    if envelope.get("clarification"):
        packet["clarification"] = envelope["clarification"]
    prefix = ("企业资料查询已结束。只依据下列证据说明结论，保留条件、冲突和缺失项，不要再次委托。\n"
              if chinese else
              "Search complete. Use only this evidence, preserve conditions, conflicts and missing facts, and do not delegate again.\n")
    content = prefix + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return content if len(content.encode()) <= max_bytes else None


def disposition(code, chinese):
    messages = {
        "UNSUPPORTED": ("当前后端只支持查询企业知识库。请告知用户这项任务不受支持。",
                        "The backend only searches company documentation. Tell the user this task is unsupported."),
        "ROUTE_UNDECLARED": ("无法确认是否需要查企业资料。请向用户澄清，请勿声称已经查询。",
                            "Ask whether the user wants company documentation. No search was performed."),
        "INPUT_UNCERTAIN": ("没能可靠确认用户问题，请请用户重述，不要猜测。",
                            "The user's question could not be confirmed. Ask them to repeat it. Do not guess."),
        "DUPLICATE": ("本次输入已经委托过，请等待结果或下一次输入，不要重复查询。",
                      "This input already has a delegation. Do not repeat the lookup."),
        "SUPERSEDED_AUDIO": ("用户已经开始新一轮输入。不要播报旧问题的结果。",
                             "The user started a new input. Do not speak the old result."),
        "PRIVATE_WEB_SEARCH": ("本会话已处理企业资料，无法执行公开搜索。请让用户开启新会话后再试。",
                               "This call has used company documents. Ask the user to start a new call for public search."),
        "TIMEOUT": ("知识库查询超时，请告知用户无法确认答案。",
                    "The documentation search timed out. Tell the user the answer could not be confirmed."),
    }
    return messages.get(code, (
        "无法取得可用的企业资料，请告知用户无法确认答案，不要编造。",
        "Usable documentation is unavailable. Say that the answer cannot be confirmed. Do not make up facts.",
    ))[0 if chinese else 1]
