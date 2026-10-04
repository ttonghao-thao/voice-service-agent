import asyncio
import base64
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass

import anyio
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.evidence import EvidenceGate, presentation_contract
from app.contracts import (
    AnswerBundle,
    BridgeArguments,
    DomainError,
    PortalAudioAppend,
    PortalEvent,
    PortalPlaybackAck,
    PortalPlaybackStop,
    PortalSessionClose,
    PresentationAssessment,
    portal_client_event_adapter,
    portal_server_event_adapter,
    printable_ascii,
    uid,
)
from app.voice.provider import (
    BRIDGE_ACK,
    BRIDGE_NAME,
    QA_ACK,
    MockVoiceAdapter,
    NvidiaVoiceChatAdapter,
    ProviderCapabilities,
    session_update,
)

logger = logging.getLogger(__name__)
TRANSCRIPT_FINAL_TIMEOUT_SECONDS = 5


@dataclass
class VoiceSession:
    id: str
    principal: object
    conversation_id: str
    epoch: int
    request_revision: int
    ticket: str
    origin: str
    expires: float
    summary: str
    task: asyncio.Task | None = None
    used: bool = False
    stopped: bool = False
    suppress_next_response: bool = False
    suppressed_responses: set[str] | None = None
    qa_mode: str = "legacy"
    answer_policy: str = "knowledge_required"
    registered_tools: tuple[str, ...] = ()
    tool_definitions: list | None = None
    response_turns: dict | None = None
    suppressed_turns: set | None = None
    capabilities: ProviderCapabilities = ProviderCapabilities()


class VoiceGateway:
    def __init__(self, settings, coordinator, store):
        self.settings, self.coordinator, self.store = settings, coordinator, store
        self.sessions = {}
        self.provider_factory = (
            MockVoiceAdapter if settings.voice_provider == "mock" else NvidiaVoiceChatAdapter
        )

    def expire(self):
        for sid, s in list(self.sessions.items()):
            if not s.used and time.monotonic() > s.expires:
                self.sessions.pop(sid, None)

    async def issue(self, principal, cid):
        self.expire()
        async with self.coordinator.lock(cid):
            await self.coordinator.ensure_owner(principal, cid)
            if self.coordinator.draining:
                raise DomainError("SERVICE_DRAINING", "Voice service is under maintenance", 503)
            s = self.settings
            if s.voice_provider == "disabled":
                raise DomainError("VOICE_UNAVAILABLE", "Voice is disabled in this deployment. Use text.", 503)
            async with self.store.transaction() as db:
                conversation = await self.store.get(db, cid, principal)
                if conversation.locale != "en-US":
                    raise DomainError("UNSUPPORTED_LOCALE", "Voice is available for English conversations only", 409)
            if s.voice_provider == "nvidia" and (
                not s.voicechat_ws_url
                or not s.voicechat_api_key.get_secret_value()
            ):
                raise DomainError("VOICE_UNAVAILABLE", "VoiceChat is not configured", 503)
            if len(self.sessions) >= s.max_voice_sessions:
                raise DomainError("VOICE_CAPACITY_EXCEEDED", "Voice capacity is full. Use text or try again later.", 429, True)
            registered_tools, tool_definitions = (), None
            capabilities = getattr(self.provider_factory, "capabilities", ProviderCapabilities())
            if conversation.qa_execution_mode == "dual_tools":
                dispatcher = self.coordinator.runtime.dispatcher
                registered_tools = await dispatcher.available(self.coordinator.runtime.registry, principal)
                if not registered_tools or (
                    "reason_over_knowledge" in registered_tools and s.agent_provider != "mock"
                    and (not self.coordinator.runtime.client or not s.agent_model)
                ):
                    raise DomainError("VOICE_UNAVAILABLE", "The registered knowledge tools are not configured", 503)
                tool_definitions = dispatcher.definitions(registered_tools, capabilities.wait_interaction)
                if len(registered_tools) > 5:
                    logger.warning("voice_tool_count_exceeds_recommendation count=%s", len(registered_tools))
            epoch, request_revision = await self.store.rotate_voice(principal, cid)
            old_task = self.coordinator.tasks.pop(cid, None)
            if old_task:
                old_task.cancel()
            await self.close_conversation(cid)
            async with self.store.transaction() as db:
                c = await self.store.get(db, cid, principal, lock=True)
                sid, ticket = uid(), secrets.token_urlsafe(32)
                c.voice_session_id = sid
                visible_history = self.coordinator.authorized_history(c.history, principal)
                from app.task_context import reconnect_summary
                deliveries = await self.store.delivery_summary(db, c, principal, {
                    value.strip() for value in s.knowledge_base_ids.split(",") if value.strip()})
                summary = reconnect_summary(c.context_state, visible_history, deliveries)
                session = VoiceSession(
                    sid,
                    principal,
                    cid,
                    epoch,
                    request_revision,
                    ticket,
                    s.public_origin,
                    time.monotonic() + 60,
                    summary,
                    suppressed_responses=set(),
                    qa_mode=c.qa_execution_mode,
                    answer_policy=c.answer_policy,
                    response_turns={},
                    suppressed_turns=set(),
                    registered_tools=registered_tools,
                    tool_definitions=tool_definitions,
                    capabilities=capabilities,
                )
            self.sessions[sid] = session
            return {
                "voice_session_id": sid,
                "epoch": epoch,
                "request_revision": request_revision,
                "wait_interaction": capabilities.wait_interaction,
                "ws_url": f"/api/v1/voice-sessions/{sid}/stream?ticket={ticket}",
            }

    async def close_conversation(self, cid):
        for sid, session in list(self.sessions.items()):
            if session.conversation_id == cid:
                session.stopped = True
                self.sessions.pop(sid, None)
                if session.task and session.task is not asyncio.current_task():
                    session.task.cancel()

    async def suppress_playback(self, cid, epoch, response_id=None):
        for session in self.sessions.values():
            if session.conversation_id != cid or session.epoch != epoch:
                continue
            if response_id:
                session.suppressed_responses.add(response_id)
            else:
                session.suppress_next_response = True

    async def suppress_turn(self, cid, epoch, turn_id):
        last_response = None
        for session in self.sessions.values():
            if session.conversation_id == cid and session.epoch == epoch:
                session.suppressed_turns.add(turn_id)
                for rid, owner in session.response_turns.items():
                    if owner[0] == turn_id:
                        session.suppressed_responses.add(rid)
                        last_response = rid
        return last_response

    async def close(self):
        tasks = []
        for session in list(self.sessions.values()):
            if session.task and session.task is not asyncio.current_task():
                session.task.cancel()
                tasks.append(session.task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.clear()

    async def stream(self, ws, sid, ticket):
        self.expire()
        session = self.sessions.get(sid)
        if (
            not session
            or session.used
            or not secrets.compare_digest(session.ticket, ticket or "")
            or ws.headers.get("origin") != session.origin
        ):
            await ws.close(code=4403)
            return
        session.used, session.task = True, asyncio.current_task()
        await ws.accept()
        provider = self.provider_factory(self.settings)
        if session.qa_mode == "dual_tools":
            provider.configuration = session_update(session.summary, session.tool_definitions, session.answer_policy)
        incoming = asyncio.Queue(maxsize=6)  # 480 ms at fixed 80 ms chunks.
        controls = asyncio.Queue(maxsize=16)
        outgoing = asyncio.Queue(maxsize=12)
        wake = asyncio.Event()
        pending, workers, sent_samples = {}, set(), {}
        input_transcripts = {}
        unbound_inputs = deque()
        consumed_inputs = set()
        authorized_responses = set()
        initial_responses = set()
        initial_text_completed = set()
        tool_responses = {}
        response_calls = {}
        response_turns = session.response_turns
        speech_segments = {}
        speech_segment_counts = {}
        ack_prefixes = {}
        last_speech_done = {}
        speech_new_delta = set()
        authorized_followups = set()
        ack_responses = set()
        general_inputs = {}
        general_deadlines = {}
        completed_responses = set()
        observed_ends = set()
        response_texts = {}
        evidence_turns = {}
        response_deliveries = {}
        call_tools, call_versions = {}, {}
        call_inputs, speech_plans, response_phases = {}, {}, {}
        call_plans, call_owners, call_operations, settlement_calls = {}, {}, {}, set()
        network_done, playback_finished = {}, set()
        latest_input_id = None
        ack_text = QA_ACK if session.qa_mode == "dual_tools" else BRIDGE_ACK
        seq, client_seq, started = 0, -1, time.monotonic()
        frames, last_input, last_ack, last_playback_stop = 0, started, 0.0, 0.0
        input_state = "quiet"

        async def current(tid=None, revision=None):
            await self.coordinator.coordination.check(session.conversation_id)
            return not session.stopped and await self.store.current(
                session.conversation_id, session.epoch, tid, revision
            ) and tid not in session.suppressed_turns

        def emit(kind, payload, turn_id=None):
            nonlocal seq
            seq += 1
            e = PortalEvent(
                type="portal." + kind,
                conversation_id=session.conversation_id,
                epoch=session.epoch,
                request_revision=session.request_revision,
                turn_id=turn_id,
                server_seq=seq,
                payload=payload,
            ).model_dump(mode="json")
            portal_server_event_adapter.validate_python(e)
            try:
                outgoing.put_nowait(e)
            except asyncio.QueueFull as exc:
                raise DomainError("AUDIO_BACKPRESSURE", "Playback fell behind. Restart voice.", 409, True) from exc

        async def writer():
            while True:
                event = await outgoing.get()
                async with self.coordinator.lock(session.conversation_id):
                    response_id = event["payload"].get("response_id")
                    boundary = event["type"] == "portal.audio.done"
                    if response_id in session.suppressed_responses and event["type"] in (
                        "portal.audio.delta", "portal.speech_text.delta", "portal.speech_text.done"):
                        continue
                    if response_id in general_inputs and general_inputs[response_id] != latest_input_id:
                        continue
                    tid, revision = event.get("turn_id"), event.get("request_revision") if event.get("turn_id") else None
                    if not tid and response_id in response_calls and response_id in response_turns:
                        tid, revision = response_turns[response_id]
                        event["turn_id"], event["request_revision"] = tid, revision
                    allowed = await current(tid, revision)
                    if boundary and tid in session.suppressed_turns:
                        allowed = await current() and await self.store.current(
                            session.conversation_id, session.epoch, tid, revision)
                    if allowed:
                        is_audio = event["type"] in ("portal.audio.delta", "portal.audio.done")
                        if is_audio:
                            parent_call = response_calls.get(response_id) or response_deliveries.get(response_id)
                            bound_input = general_inputs.get(response_id) or call_inputs.get(parent_call)
                            audit_revision = revision if tid else None
                            if response_id not in sent_samples and not await self.store.delivery(session.conversation_id, session.epoch, response_id,
                                "voice_audio", "write_started", tid, audit_revision, response_id,
                                input_item_id=bound_input, phase=response_phases.get(response_id, "unclassified")):
                                continue
                        async with asyncio.timeout(2):
                            await ws.send_json(event)
                        if event["type"] == "portal.audio.delta":
                            sent_samples[response_id] = sent_samples.get(response_id, 0) + len(
                                base64.b64decode(event["payload"]["audio"], validate=True)) // 2
                            await self.store.delivery(session.conversation_id, session.epoch, response_id,
                                "voice_audio", "sent", tid, audit_revision, response_id, sent_samples=sent_samples[response_id])
                        elif boundary:
                            await self.store.delivery(session.conversation_id, session.epoch, response_id,
                                "voice_audio", "completed", tid, audit_revision, response_id,
                                sent_samples=sent_samples.get(response_id, 0), phase=event["payload"]["phase"])
                            network_done.setdefault(response_id, asyncio.Event()).set()

        async def upstream_writer():
            while True:
                await wake.wait()
                wake.clear()
                while not controls.empty() or not incoming.empty():
                    if not await current():
                        return
                    if not controls.empty():
                        call, tid, revision, text, authorize_output = controls.get_nowait()
                        # This is the final fence immediately at the single writer.
                        async with self.coordinator.lock(session.conversation_id):
                            if authorize_output is None:
                                if (session.capabilities.wait_interaction and call in settlement_calls
                                    and pending.get(call) == "settlement_ready"
                                    and await current(revision=revision)
                                    and await self.store.tool_settlement(session.conversation_id, session.epoch,
                                        call, tid, revision, "write_started")):
                                    pending[call] = "settled"
                                    async with asyncio.timeout(2):
                                        await provider.submit_tool_result(call, text)
                                    await self.store.tool_settlement(session.conversation_id, session.epoch,
                                        call, tid, revision, "sent")
                                continue
                            if await current(tid, revision) and pending.get(call) == "ready":
                                if session.qa_mode == "dual_tools":
                                    allowed = await self.coordinator.runtime.registry.allowed(session.principal)
                                    definition = self.coordinator.runtime.dispatcher.tools.get(call_tools.get(call))
                                    if (not definition or not definition.required_tools <= allowed
                                        or definition.permission_scope not in session.principal.scopes
                                        or (session.principal.expires_at is not None and session.principal.expires_at <= time.time())):
                                        raise DomainError("FORBIDDEN", "Tool access changed before delivery", 403)
                                    for name, version in call_versions.get(call, {}).items():
                                        if await self.store.tool_revision(name) != version:
                                            raise DomainError("FORBIDDEN", "Tool configuration changed before delivery", 403)
                                    if not await self.store.delivery(session.conversation_id, session.epoch, call,
                                            "tool_result", "write_started", tid, revision):
                                        continue
                                # Install the one-response grant before the network write can yield.
                                # A fast provider may start answering before submit returns.
                                pending[call] = "sent"
                                if authorize_output:
                                    authorized_followups.add(tool_responses.get(call, call))
                                async with asyncio.timeout(2):
                                    await provider.submit_tool_result(call, text)
                                if session.qa_mode == "dual_tools":
                                    await self.store.delivery(session.conversation_id, session.epoch, call,
                                        "tool_result", "sent", tid, revision)
                                logger.info(
                                    "voice_tool_result_submitted conversation_id=%s turn_id=%s call_id=%s",
                                    session.conversation_id,
                                    tid,
                                    call,
                                )
                    else:
                        audio = incoming.get_nowait()
                        async with asyncio.timeout(2):
                            await provider.send_audio(audio)

        def transcript_future(item_id):
            future = input_transcripts.get(item_id)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                input_transcripts[item_id] = future
            return future

        def prune_inputs():
            for item_id, future in list(input_transcripts.items()):
                if item_id in consumed_inputs and future.done():
                    input_transcripts.pop(item_id, None)
                    consumed_inputs.discard(item_id)
                    try:
                        unbound_inputs.remove(item_id)
                    except ValueError:
                        pass

        async def bridge(payload, input_item_id, final_transcript):
            call_id = payload["call_id"]
            tid = None
            revision = session.request_revision
            try:
                logger.info(
                    "voice_bridge_started conversation_id=%s call_id=%s",
                    session.conversation_id,
                    call_id,
                )
                decision = None
                if session.qa_mode == "dual_tools":
                    decision = self.coordinator.runtime.dispatcher.resolve(
                        payload["name"], payload["arguments"], session.registered_tools,
                        session.capabilities.wait_interaction)
                    args = decision.arguments
                elif (
                    payload["name"] != BRIDGE_NAME
                    or not isinstance(payload["arguments"], str)
                    or len(payload["arguments"]) > 12000
                ):
                    raise ValueError("Unsupported bridge")
                else:
                    args = BridgeArguments.model_validate_json(payload["arguments"])
                request = (
                    await asyncio.wait_for(
                        asyncio.shield(final_transcript),
                        TRANSCRIPT_FINAL_TIMEOUT_SECONDS,
                    )
                ).strip()
                if not request or len(request) > 2000:
                    raise ValueError("Invalid final transcript")
                operation = getattr(args, "operation", "query")
                revision_of = None
                if operation in ("progress", "revise"):
                    progress = await self.coordinator.task_progress(session.principal,
                        session.conversation_id, session.epoch, session.request_revision)
                    if operation == "progress":
                        bundle = AnswerBundle(status="answered", display_text=progress.message,
                            speech_text=progress.message, answer_kind="general", composition="provider_general")
                        call_plans[call_id] = EvidenceGate.prepare(bundle)
                        pending[call_id] = "ready"
                        await self.store.delivery(session.conversation_id, session.epoch, call_id,
                            "tool_result", "prepared", revision=session.request_revision, input_item_id=input_item_id, phase="progress")
                        controls.put_nowait((call_id, None, session.request_revision,
                            json.dumps({"status": progress.status, "speech_text": bundle.speech_text,
                                        "language": "en-US", "operation": "progress"}), True))
                        wake.set()
                        return
                    if progress.status != "running" or not progress.turn_id:
                        raise DomainError("STALE_REVISION", "There is no pending request to revise", 409)
                    revision_of = (progress.turn_id, progress.request_revision)
                logger.info(
                    "voice_transcript_bound conversation_id=%s call_id=%s input_item_id=%s arguments_match=%s",
                    session.conversation_id,
                    call_id,
                    input_item_id,
                    request == getattr(args, "user_request", "").strip(),
                )
                turn, task = await self.coordinator.submit(
                    session.principal,
                    session.conversation_id,
                    f"voice:{session.epoch}:{call_id}",
                    request,
                    "voice",
                    session.epoch,
                    call_id,
                    input_item_id,
                    decision,
                    revision_of,
                )
                tid = turn.id
                revision = turn.request_revision
                session.request_revision = revision
                call_owners[call_id] = (tid, revision)
                if revision_of:
                    for old_call, old_owner in list(call_owners.items()):
                        if old_owner != revision_of:
                            continue
                        authorized_followups.discard(tool_responses.get(old_call))
                        session.suppressed_turns.add(old_owner[0])
                        for rid, owner in response_turns.items():
                            if owner == old_owner:
                                session.suppressed_responses.add(rid)
                        if pending.get(old_call) in ("running", "ready"):
                            pending[old_call] = "settlement_ready"
                            settlement_calls.add(old_call)
                            controls.put_nowait((old_call, old_owner[0], revision,
                                json.dumps({"status": "canceled", "reason_code": "REQUEST_REVISED",
                                            "speech_text": "", "instructions": "Discard this superseded request. Produce no speech."}), None))
                        else:
                            pending[old_call] = "superseded"
                    emit("playback.clear", {"message": "The pending request was revised."})
                    wake.set()
                response_turns[tool_responses[call_id]] = (tid, revision)
                for rid, parent_call in response_calls.items():
                    if parent_call == call_id:
                        response_turns[rid] = (tid, revision)
                bundle = await task if task else None
                if not bundle or not await current(tid, revision):
                    return
                if isinstance(bundle, EvidenceReady):
                    evidence_turns[tid] = bundle
                    text = json.dumps(bundle.envelope, ensure_ascii=True)
                    pending[call_id] = "ready"
                    await self.store.delivery(session.conversation_id, session.epoch, call_id,
                                              "tool_result", "prepared", tid, revision)
                    controls.put_nowait((call_id, tid, revision, text, True))
                    wake.set()
                    return
                speech_plans[tid] = bundle if bundle.presentation else EvidenceGate.prepare(bundle)
                logger.info(
                    "voice_bridge_finished conversation_id=%s turn_id=%s call_id=%s status=%s",
                    session.conversation_id,
                    tid,
                    call_id,
                    bundle.status,
                )
                if bundle.speech_language != "en-US" or not printable_ascii(bundle.speech_text):
                    text = json.dumps({
                        "status": "failed",
                        "speech_text": "Please read the written response in the portal. I cannot speak it safely.",
                        "language": "en-US",
                        "is_mock": bundle.is_mock,
                    })
                else:
                    text = json.dumps(
                        {
                            "status": bundle.status,
                            "speech_text": bundle.speech_text,
                            "language": bundle.speech_language,
                            "is_mock": bundle.is_mock,
                            "presentation": bundle.presentation.model_dump(),
                        },
                        ensure_ascii=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "voice_bridge_failed conversation_id=%s turn_id=%s call_id=%s exception_type=%s",
                    session.conversation_id,
                    tid or "none",
                    call_id,
                    type(exc).__name__,
                )
                text = json.dumps(
                    {
                        "status": "failed",
                        "speech_text": "I could not confirm the completed request. Please ask again.",
                        "language": "en-US",
                    },
                    ensure_ascii=True,
                )
            if await current(tid, revision):
                pending[call_id] = "ready"
                if session.qa_mode == "dual_tools":
                    await self.store.delivery(session.conversation_id, session.epoch, call_id,
                                              "tool_result", "prepared", tid, revision)
                authorize = tid is not None or session.qa_mode == "legacy"
                if not authorize:
                    session.suppressed_responses.add(tool_responses[call_id])
                controls.put_nowait((call_id, tid, revision, text, authorize))
                wake.set()

        async def receiver():
            nonlocal input_state, latest_input_id
            customer_input_seen = False
            async for event in provider.events():
                if not await current():
                    return
                if event.kind == "tool":
                    call = event.payload["call_id"]
                    if call in pending:
                        continue
                    if (
                        event.payload.get("name") not in (
                            session.registered_tools if session.qa_mode == "dual_tools" else (BRIDGE_NAME,))
                        or not isinstance(event.payload.get("arguments"), str)
                        or len(event.payload["arguments"]) > 12000
                    ):
                        raise DomainError(
                            "VOICE_PROTOCOL_ERROR",
                            "Voice requested an unsupported business tool",
                            502,
                        )
                    if len(pending) >= 128:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Too many voice tool calls", 502)
                    operation = "query"
                    if session.capabilities.wait_interaction and session.qa_mode == "dual_tools":
                        operation = getattr(self.coordinator.runtime.dispatcher.resolve(
                            event.payload["name"], event.payload["arguments"], session.registered_tools, True).arguments,
                            "operation", "query")
                    if any(v in ("running", "ready", "settlement_ready") for v in pending.values()):
                        if not session.capabilities.wait_interaction or operation not in ("progress", "revise"):
                            raise DomainError("VOICE_PROTOCOL_ERROR", "Parallel cloud tool calls are unsupported. Please ask again.", 502)
                    response_id = event.payload.get("response_id")
                    call_tools[call] = event.payload.get("name")
                    call_operations[call] = operation
                    if session.qa_mode == "dual_tools":
                        definition = self.coordinator.runtime.dispatcher.tools[call_tools[call]]
                        call_versions[call] = {name: await self.store.tool_revision(name)
                                               for name in definition.required_tools}
                    tool_responses[call] = response_id or call
                    if response_id:
                        response_calls[response_id] = call
                    input_item_id = general_inputs.get(response_id) or next(
                        (item for item in unbound_inputs if item not in consumed_inputs),
                        None,
                    )
                    if input_item_id is None:
                        pending[call] = "ready"
                        if response_id:
                            session.suppressed_responses.add(response_id)
                        controls.put_nowait(
                            (
                                call,
                                None,
                                session.request_revision,
                                json.dumps(
                                    {
                                        "status": "failed",
                                        "speech_text": "No completed customer request was received. Please wait for the customer to speak.",
                                        "language": "en-US",
                                    },
                                    ensure_ascii=True,
                                ),
                                False,
                            )
                        )
                        wake.set()
                        logger.warning(
                            "voice_unbound_tool_settled conversation_id=%s call_id=%s",
                            session.conversation_id,
                            call,
                        )
                        continue
                    final_transcript = transcript_future(input_item_id)
                    for rid, associated_input in list(general_inputs.items()):
                        if associated_input == input_item_id:
                            general_inputs.pop(rid)
                            response_calls[rid] = call
                    consumed_inputs.add(input_item_id)
                    call_inputs[call] = input_item_id
                    prune_inputs()
                    if response_id:
                        general_inputs.pop(response_id, None)
                        authorized_responses.add(response_id)
                    logger.info(
                        "voice_tool_call_received conversation_id=%s call_id=%s tool=%s input_item_id=%s",
                        session.conversation_id,
                        call,
                        event.payload.get("name"),
                        input_item_id,
                    )
                    pending[call] = "running"
                    task = asyncio.create_task(
                        bridge(
                            event.payload,
                            input_item_id,
                            final_transcript,
                        )
                    )
                    workers.add(task)
                    task.add_done_callback(workers.discard)
                    continue
                if event.kind == "session.ended":
                    return
                if event.kind == "input.state":
                    input_state = event.payload["state"]
                    if input_state == "speaking":
                        customer_input_seen = True
                        item_id = event.payload["item_id"]
                        latest_input_id = item_id
                        if item_id not in input_transcripts:
                            prune_inputs()
                            if len(input_transcripts) >= 16:
                                raise DomainError(
                                    "VOICE_PROTOCOL_ERROR",
                                    "Too many unfinished voice inputs",
                                    502,
                                )
                            transcript_future(item_id)
                            unbound_inputs.append(item_id)
                    emit("input.state", {"state": input_state})
                    continue
                if event.kind == "transcript.done":
                    customer_input_seen = True
                    item_id = event.payload["item_id"]
                    if item_id not in input_transcripts:
                        latest_input_id = item_id
                    if session.qa_mode == "dual_tools":
                        await self.store.utterance(session.principal, session.conversation_id,
                                                   session.epoch, item_id, event.payload["text"])
                    if item_id not in input_transcripts:
                        prune_inputs()
                        if len(input_transcripts) >= 16:
                            raise DomainError(
                                "VOICE_PROTOCOL_ERROR",
                                "Too many unfinished voice inputs",
                                502,
                            )
                        unbound_inputs.append(item_id)
                    future = transcript_future(item_id)
                    if not future.done():
                        future.set_result(event.payload["text"])
                    prune_inputs()
                response_id = event.payload.get("response_id")
                parent_call = event.payload.pop("parent_call_id", None)
                if parent_call and event.kind.startswith(("speech_text", "audio")):
                    if not session.capabilities.correlated_tool_output or parent_call not in pending:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Unverified voice output association", 502)
                    if pending[parent_call] in ("settlement_ready", "settled", "superseded"):
                        continue
                    owner = call_owners.get(parent_call)
                    if owner and not await current(*owner):
                        continue
                stale_owner = response_turns.get(response_id)
                if stale_owner and event.kind.startswith(("speech_text", "audio")) and not await current(*stale_owner):
                    continue
                if response_id in general_inputs and general_inputs[response_id] != latest_input_id:
                    continue
                if response_id in observed_ends and event.kind.startswith(("speech_text", "audio")):
                    if event.kind == "audio.done":
                        continue  # duplicate terminal signal, never a new output grant
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Voice reused an ended audio response. Restart voice or use text.", 502)
                if event.kind == "speech_text.delta":
                    speech_new_delta.add(response_id)
                if event.kind == "speech_text.done":
                    if (
                        last_speech_done.get(response_id) == event.payload["text"]
                        and response_id not in speech_new_delta
                    ):
                        continue
                    last_speech_done[response_id] = event.payload["text"]
                    speech_new_delta.discard(response_id)
                # Some VoiceChat builds keep their initial (often silent) audio
                # response open for the entire connection. Never silently drop
                # a later spoken answer under that permanently suppressed ID,
                # or authorize the whole connection as though it were one turn.
                if (
                    response_id in initial_responses
                    and response_id in initial_text_completed
                    and customer_input_seen
                    and event.kind.startswith("speech_text")
                    and event.payload.get("text", "").strip()
                ):
                    logger.warning(
                        "voice_response_lifecycle_mismatch conversation_id=%s response_id=%s",
                        session.conversation_id,
                        response_id,
                    )
                    raise DomainError(
                        "VOICE_PROTOCOL_ERROR",
                        "VoiceChat reused its initial audio response for a customer reply. "
                        "Check the VoiceChat response lifecycle; use text for now.",
                        502,
                    )
                known_owner = response_turns.get(response_id)
                if (known_owner and event.kind.startswith(("speech_text", "audio"))
                    and not await current(*known_owner)):
                    # A late frame with an already-bound identity must never
                    # consume the continuation permission of the new task.
                    continue
                for parent in list(authorized_followups):
                    owner = response_turns.get(parent)
                    if owner and not await current(*owner):
                        authorized_followups.discard(parent)
                if (
                    response_id
                    and event.kind.startswith(("speech_text", "audio"))
                    and response_id not in (session.suppressed_responses or set())
                ):
                    if response_id not in authorized_responses and authorized_followups:
                        if session.capabilities.correlated_tool_output:
                            parent_response = tool_responses.get(parent_call)
                            if parent_response not in authorized_followups:
                                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice continuation lacks its tool association", 502)
                            authorized_followups.remove(parent_response)
                        else:
                            if len(authorized_followups) != 1:
                                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice continuation is ambiguous", 502)
                            parent_response = authorized_followups.pop()
                        authorized_responses.add(response_id)
                        if parent_response in response_turns:
                            response_turns[response_id] = response_turns[parent_response]
                        response_deliveries[response_id] = response_calls.get(parent_response)
                        if call_operations.get(response_deliveries[response_id]) == "progress":
                            general_inputs[response_id] = call_inputs[response_deliveries[response_id]]
                        if session.qa_mode == "dual_tools" and response_deliveries[response_id]:
                            owner = response_turns.get(response_id)
                            await self.store.delivery(session.conversation_id, session.epoch,
                                response_deliveries[response_id], "tool_result", None,
                                owner[0] if owner else None, owner[1] if owner else session.request_revision,
                                response_id)
                    if (
                        session.qa_mode == "dual_tools" and session.answer_policy == "general_qa"
                        and response_id not in authorized_responses and customer_input_seen
                        and response_id not in completed_responses
                        and not any(v in ("running", "ready") for v in pending.values())
                    ):
                        candidates = [item for item in unbound_inputs if item not in consumed_inputs]
                        if len(candidates) == 1 and candidates[0] == latest_input_id:
                            general_inputs[response_id] = candidates[0]
                            general_deadlines[response_id] = time.monotonic() + self.settings.qa_provider_answer_timeout_ms / 1000
                            authorized_responses.add(response_id)
                    if response_id not in authorized_responses and customer_input_seen:
                        logger.warning(
                            "voice_unbridged_response_rejected conversation_id=%s response_id=%s",
                            session.conversation_id,
                            response_id,
                        )
                        raise DomainError(
                            "VOICE_TOOL_REQUIRED",
                            "Voice did not reach the knowledge assistant. Restart voice or use text.",
                            502,
                            True,
                        )
                    if response_id not in authorized_responses:
                        initial_responses.add(response_id)
                        session.suppressed_responses.add(response_id)
                        logger.info(
                            "voice_initial_response_suppressed conversation_id=%s response_id=%s",
                            session.conversation_id,
                            response_id,
                        )
                if response_id and session.suppress_next_response:
                    session.suppressed_responses.add(response_id)
                suppressed = bool(
                    response_id and response_id in (session.suppressed_responses or set())
                )
                if response_id in general_inputs and general_inputs[response_id] != latest_input_id:
                    session.suppressed_responses.add(response_id)
                    suppressed = True
                turn_owner = response_turns.get(response_id)
                if (
                    turn_owner
                    and event.kind.startswith(("speech_text", "audio"))
                    and not await current(*turn_owner)
                ):
                    continue
                if event.kind.startswith("speech_text") and not suppressed:
                    # The configured ACK can finish after a fast tool result.
                    # Hold only its exact prefix; release a divergent answer
                    # immediately so normal speech still streams.
                    if (
                        event.kind == "speech_text.delta"
                        and response_id in response_calls
                        and pending.get(response_calls[response_id]) == "sent"
                        and response_id not in speech_segments
                    ):
                        candidate = ack_prefixes.get(response_id, "") + event.payload["text"]
                        if ack_text.startswith(candidate):
                            ack_prefixes[response_id] = candidate
                            continue
                        if response_id in ack_prefixes:
                            event.payload = {**event.payload, "text": candidate}
                            ack_prefixes.pop(response_id, None)
                    phase = speech_segments.setdefault(
                        response_id,
                        "status"
                        if (
                            response_id in response_calls
                            and pending.get(response_calls[response_id]) != "sent"
                        ) or (
                            event.kind == "speech_text.done"
                            and " ".join(event.payload["text"].split()) == ack_text
                        )
                        else "answer",
                    )
                    if event.kind == "speech_text.done":
                        ack_prefixes.pop(response_id, None)
                    event.payload = {
                        **event.payload,
                        "phase": phase,
                        "segment_index": speech_segment_counts.get(response_id, 0),
                    }
                    if session.qa_mode == "dual_tools":
                        event.payload["answer_kind"] = "knowledge" if turn_owner else "general"
                        event.payload["input_item_id"] = general_inputs.get(response_id) or call_inputs.get(
                            response_calls.get(response_id) or response_deliveries.get(response_id))
                    previous_phase = response_phases.get(response_id)
                    response_phases[response_id] = phase if previous_phase in (None, phase) else "mixed"
                if (
                    event.kind.endswith(".done")
                    and "text" in event.payload
                    and not suppressed
                    and (event.kind != "speech_text.done" or ((turn_owner or response_id in general_inputs)
                         and event.payload["phase"] == "answer"))
                ):
                    kind = "voicechat_transcript" if event.kind.startswith("speech") else "user_transcript"
                    source = (
                        f"{response_id}:{event.payload['segment_index']}"
                        if kind == "voicechat_transcript"
                        else event.payload["item_id"]
                    )
                    stored_payload = dict(event.payload)
                    if kind == "voicechat_transcript":
                        stored_payload["turn_id"] = turn_owner[0] if turn_owner else None
                        stored_payload["_authorized_kb_ids"] = sorted(
                            session.principal.knowledge_base_ids if turn_owner else ()
                        )
                    if not await self.store.record(
                        session.conversation_id, session.epoch, kind, source, stored_payload,
                        turn_owner[0] if kind == "voicechat_transcript" and turn_owner else None,
                        turn_owner[1] if kind == "voicechat_transcript" and turn_owner else None,
                    ):
                        continue
                    if kind == "voicechat_transcript":
                        response_texts.setdefault(response_id, []).append(event.payload["text"])
                if (event.kind == "speech_text.done" and suppressed and turn_owner
                    and turn_owner[0] in evidence_turns
                    and " ".join(event.payload["text"].split()) != ack_text):
                    # Playback suppression is not task cancellation. Retain the
                    # current task's text for evidence checks without forwarding
                    # speech or recording it as a presented response.
                    response_texts.setdefault(response_id, []).append(event.payload["text"])
                if event.kind == "audio.delta":
                    audio = base64.b64decode(event.payload["audio"], validate=True)
                    if len(audio) % 2 or len(audio) > 48000:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice audio frame is not PCM16", 502)
                    response_id = event.payload["response_id"]
                if event.kind == "speech_text.done":
                    speech_segments.pop(response_id, None)
                    speech_segment_counts[response_id] = speech_segment_counts.get(response_id, 0) + 1
                    if response_id in initial_responses:
                        initial_text_completed.add(response_id)
                    if " ".join(event.payload["text"].split()) == ack_text:
                        ack_responses.add(response_id)
                    else:
                        ack_responses.discard(response_id)
                if event.kind == "audio.done":
                    observed_ends.add(response_id)
                    event.payload = {
                        **event.payload,
                        "phase": "status" if response_id in ack_responses else "answer",
                    }
                    # A fast business result can arrive while the fixed tool ACK
                    # is still playing. ACK frames must not spend the permission
                    # reserved for the following answer. If the original response
                    # itself contains the answer, retire that spare permission.
                    if response_id not in ack_responses:
                        authorized_followups.discard(response_id)
                        text = " ".join(response_texts.get(response_id, []))
                        plan = speech_plans.get(turn_owner[0]) if turn_owner else call_plans.get(
                            response_deliveries.get(response_id) or response_calls.get(response_id))
                        ready = evidence_turns.get(turn_owner[0]) if turn_owner else None
                        general_bundle = None
                        if response_id in general_inputs and not suppressed:
                            general_bundle = plan or AnswerBundle(
                                status="answered" if text else "failed", display_text=text, speech_text="",
                                answer_kind="general", composition="provider_general",
                                validation_level="provider_only", verification_timing="not_verified",
                                is_mock=self.settings.voice_provider == "mock")
                        contract = plan.presentation if plan else presentation_contract("grounded", {
                            key: entry["value"] for key, entry in ready.envelope.get("task_context", {}).get("conditions", {}).items()
                        }) if ready else None
                        code = EvidenceGate.check_presentation(text, contract,
                            ready.citations if ready else (), plan.speech_text if plan else None) if contract else None
                        assessment = PresentationAssessment(response_id=response_id,
                            answer_id=plan.answer_id if plan else ready.answer_id if ready else general_bundle.answer_id if general_bundle else None,
                            input_item_id=general_inputs.get(response_id) or call_inputs.get(
                                response_deliveries.get(response_id) or response_calls.get(response_id)),
                            mode=contract.mode if contract else "unverified",
                            status="failed" if code else "matched" if contract else "unverified", reason_code=code)
                        if turn_owner or response_id in general_inputs:
                            await self.store.presentation(session.conversation_id, session.epoch,
                                turn_owner[0] if turn_owner else None, turn_owner[1] if turn_owner else None,
                                assessment, session.principal.knowledge_base_ids if turn_owner else ())
                            emit("presentation.updated", assessment.model_dump(), turn_owner[0] if turn_owner else None)
                        if code and plan:
                            # The written answer remains usable; already-sent audio cannot be revoked.
                            session.suppressed_responses.add(response_id)
                            await self.store.suppress_deliveries(session.conversation_id, session.epoch,
                                                                response_id, code)
                            emit("playback.clear", {"response_id": response_id,
                                "message": "The spoken reply differs from its presentation constraints. Read the written answer."}, turn_owner[0])
                        if turn_owner and turn_owner[0] in evidence_turns:
                            self.coordinator.provider_answer(turn_owner[0], " ".join(response_texts.get(response_id, [])))
                        elif response_id in general_inputs and not suppressed:
                            await self.store.utterance(session.principal, session.conversation_id,
                                session.epoch, general_inputs[response_id], answer=general_bundle)
                            consumed_inputs.add(general_inputs[response_id])
                        completed_responses.add(response_id)
                    ack_responses.discard(response_id)
                    authorized_responses.discard(response_id)

                if suppressed:
                    if event.kind == "audio.done":
                        initial_responses.discard(response_id)
                        initial_text_completed.discard(response_id)
                        session.suppressed_responses.discard(response_id)
                        session.suppress_next_response = False
                    continue
                if (event.kind.startswith("speech_text") and event.payload["phase"] == "answer"
                    and not turn_owner and response_id not in general_inputs):
                    continue
                emit(event.kind, event.payload, turn_owner[0] if turn_owner else None)

        async def browser():
            nonlocal client_seq, frames, last_input, last_ack, last_playback_stop
            while True:
                raw = await ws.receive_text()
                if len(raw) > 10000:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Audio event exceeds the size limit", 400)
                try:
                    data = portal_client_event_adapter.validate_json(raw)
                except Exception as exc:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Invalid portal voice event", 400) from exc
                if not await current():
                    return
                if data.epoch != session.epoch:
                    continue
                if isinstance(data, PortalAudioAppend):
                    index = data.seq
                    if index <= client_seq:
                        continue
                    if client_seq >= 0 and index != client_seq + 1:
                        raise DomainError("AUDIO_BACKPRESSURE", "Audio was interrupted. Restart voice.", 409)
                    client_seq = index
                    frames += 1
                    last_input = time.monotonic()
                    if frames * 0.08 - (last_input - started) > 0.5:
                        raise DomainError("AUDIO_BACKPRESSURE", "Audio was sent too quickly. Restart voice.", 409)
                    try:
                        incoming.put_nowait(data.payload.audio)
                    except asyncio.QueueFull as exc:
                        raise DomainError("AUDIO_BACKPRESSURE", "Input audio fell behind. Restart voice.", 409) from exc
                    wake.set()
                elif isinstance(data, PortalPlaybackAck):
                    response_id, samples = data.payload.response_id, data.payload.played_samples
                    if data.payload.finished:
                        if response_id not in completed_responses and response_phases.get(response_id) != "status":
                            raise DomainError("VOICE_PROTOCOL_ERROR", "Playback completed before the response ended", 400)
                        async with asyncio.timeout(2):
                            await network_done.setdefault(response_id, asyncio.Event()).wait()
                    if samples > sent_samples.get(response_id, -1):
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Playback acknowledgement exceeds sent audio", 400)
                    if data.payload.finished or time.monotonic() - last_ack > 0.5:
                        await self.store.playback_delivery(session.conversation_id, session.epoch,
                                                           response_id, samples, data.payload.finished)
                        if data.payload.finished:
                            playback_finished.add(response_id)
                        await self.store.record(
                            session.conversation_id,
                            session.epoch,
                            "playback_ack",
                            response_id,
                            {"response_id": response_id, "played_samples": samples,
                             "finished": data.payload.finished, "estimated": True},
                        )
                        last_ack = time.monotonic()
                elif isinstance(data, PortalPlaybackStop):
                    if time.monotonic() - last_playback_stop < 1:
                        continue
                    last_playback_stop = time.monotonic()
                    await self.coordinator.stop_playback(
                        session.principal,
                        session.conversation_id,
                        session.epoch,
                        session.request_revision,
                        data.payload.response_id,
                    )
                    emit(
                        "playback.clear",
                        {"message": "Playback stopped", "response_id": data.payload.response_id},
                    )
                elif data.type == "portal.interrupt":
                    await self.coordinator.interrupt(
                        session.principal, session.conversation_id, session.epoch
                    )
                    return
                elif isinstance(data, PortalSessionClose):
                    return
                else:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Unknown portal voice event", 400)

        async def watchdog():
            while True:
                await asyncio.sleep(1)
                for response_id, deadline in general_deadlines.items():
                    if response_id in general_inputs and response_id not in completed_responses and time.monotonic() > deadline:
                        await self.store.utterance(session.principal, session.conversation_id,
                            session.epoch, general_inputs[response_id], answer=AnswerBundle(
                                status="failed", display_text="The voice response timed out. Please try again.",
                                speech_text="", reason_code="VOICE_ANSWER_TIMEOUT", answer_kind="general",
                                composition="provider_general", validation_level="provider_only"))
                        raise DomainError("VOICE_ANSWER_TIMEOUT", "The voice response timed out. Please try again.", 502, True)
                if session.principal.expires_at is not None and session.principal.expires_at <= time.time():
                    raise DomainError("AUTH_REQUIRED", "Your session expired. Please sign in again.", 401)
                if not await current():
                    return
                if time.monotonic() - last_input > 5:
                    raise DomainError("AUDIO_BACKPRESSURE", "Audio capture stalled. Restart voice.", 409, True)
                elapsed = time.monotonic() - started
                if elapsed > self.settings.voice_session_max_seconds and input_state == "quiet" and not any(
                    value in ("running", "ready") for value in pending.values()
                ) and all(rid in playback_finished or rid in session.suppressed_responses
                          for rid, samples in sent_samples.items() if samples):
                    raise DomainError(
                        "VOICE_SESSION_ROTATION_REQUIRED",
                        "Voice session reached its rotation limit and is reconnecting",
                        409,
                        True,
                    )
                if elapsed > self.settings.voice_session_max_seconds + 30:
                    raise DomainError(
                        "VOICE_SESSION_EXPIRED", "Voice session exceeded the rotation grace period. Restart voice.", 409, True
                    )

        tasks = []
        error = None
        try:
            if getattr(provider, "capabilities", ProviderCapabilities()) != session.capabilities:
                raise DomainError("VOICE_PROTOCOL_ERROR", "Voice capabilities changed after session creation", 502)
            await provider.connect(session.summary)
            emit(
                "session.ready",
                {
                    "sample_rate": 24000,
                    "format": "pcm16",
                    "chunk_ms": 80,
                    "is_mock": self.settings.voice_provider == "mock",
                },
            )
            tasks = [
                asyncio.create_task(fn()) for fn in (writer, upstream_writer, receiver, browser, watchdog)
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except DomainError as exc:
            error = exc
        except asyncio.CancelledError:
            pass
        except Exception:
            error = DomainError("VOICE_UNAVAILABLE", "Voice connection ended. Restart voice or use text.", 503, True)
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks + list(workers):
                    task.cancel()
                await asyncio.gather(*tasks, *workers, return_exceptions=True)
                end_confirmed = False
                try:
                    async with asyncio.timeout(3):
                        end_confirmed = await provider.close() is True
                except Exception:
                    pass
                await self.store.settle_deliveries(session.conversation_id, session.epoch, "connection_closed")
                await self.store.finish_voice_session(session.conversation_id, session.epoch, session.id, end_confirmed)
                if error:
                    try:
                        async with asyncio.timeout(1):
                            event = PortalEvent(
                                    type="portal.error",
                                    conversation_id=session.conversation_id,
                                    epoch=session.epoch,
                                    request_revision=session.request_revision,
                                    payload=error.payload(),
                                ).model_dump(mode="json")
                            portal_server_event_adapter.validate_python(event)
                            await ws.send_json(event)
                    except Exception:
                        pass
                self.sessions.pop(sid, None)
                if session.conversation_id in self.coordinator.coordination.leases:
                    try:
                        await self.coordinator.coordination.check(session.conversation_id)
                        await self.coordinator.interrupt(
                            session.principal, session.conversation_id, session.epoch, "connection_closed"
                        )
                    except DomainError:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
