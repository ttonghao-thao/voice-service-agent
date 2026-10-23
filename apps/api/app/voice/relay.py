"""Native CueKB voice: one continuous provider connection and an async tool worker.

Response IDs identify transport segments, not permission to speak. Only actual
native calls execute tools. Conversation/credential checks remain at both writes.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time

import anyio
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.native_input import ANSWER_INSTRUCTIONS, FINAL_INSTRUCTIONS, NativeInputSearch
from app.contracts import (
    AnswerBundle,
    DomainError,
    PortalAudioAppend,
    PortalEvent,
    PortalPlaybackAck,
    PortalPlaybackStop,
    PortalSessionClose,
    portal_client_event_adapter,
    portal_server_event_adapter,
)
from app.voice.batches import ToolBatches
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)


async def relay(gateway, ws, session, provider):
    store, coordinator = gateway.store, gateway.coordinator
    cid, epoch = session.conversation_id, session.epoch
    outgoing = asyncio.Queue(maxsize=12)
    calls = asyncio.Queue(maxsize=16)
    upstream_lock = asyncio.Lock()
    inputs, seen_calls, responses, tool_inputs = {}, {}, {}, {}
    searches = {}
    batches = ToolBatches()
    terminal_sent = set()
    last_results = {}
    latest_input = None
    seq, client_seq, frames = 0, -1, 0
    started = last_input = time.monotonic()
    input_state = "quiet"
    executing = False
    tasks = []
    error = None

    async def current():
        await coordinator.coordination.check(cid)
        if session.principal.expires_at is not None and session.principal.expires_at <= time.time():
            raise DomainError("AUTH_REQUIRED", "Your session expired. Please sign in again.", 401)
        if session.stopped or not await store.current(cid, epoch):
            raise DomainError("STALE_EPOCH", "This voice connection has expired", 409)

    def emit(kind, payload):
        nonlocal seq
        seq += 1
        event = PortalEvent(type="portal." + kind, conversation_id=cid, epoch=epoch,
            request_revision=session.request_revision, server_seq=seq, payload=payload).model_dump(mode="json")
        portal_server_event_adapter.validate_python(event)
        try:
            outgoing.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise DomainError("AUDIO_BACKPRESSURE", "Playback fell behind. Restart voice.", 409, True) from exc

    def input_future(item_id):
        if item_id not in inputs:
            if len(inputs) >= 512:
                raise DomainError("VOICE_SESSION_ROTATION_REQUIRED", "Reconnect to continue the conversation.", 409, True)
            inputs[item_id] = asyncio.get_running_loop().create_future()
        return inputs[item_id]

    async def writer():
        while True:
            event = await outgoing.get()
            await current()
            body, kind = event["payload"], event["type"]
            rid = body.get("response_id")
            response = responses.get(rid)
            if rid in session.suppressed_responses and kind in (
                "portal.audio.delta", "portal.speech_text.delta", "portal.speech_text.done"):
                continue
            async with asyncio.timeout(2):
                await ws.send_json(event)
            if kind == "portal.audio.delta":
                if response["sent"] == 0:
                    logger.info("voice_audio_first_sent conversation_id=%s response_id=%s input_item_id=%s",
                        cid, rid, response["input"])
                response["sent"] += len(base64.b64decode(body["audio"])) // 2
            elif kind == "portal.speech_text.delta" and body.get("phase") == "answer" and body.get("text"):
                if not response.get("text_logged"):
                    response["text_logged"] = True
                    logger.info("voice_text_first_sent conversation_id=%s response_id=%s input_item_id=%s",
                        cid, rid, response["input"])
            elif kind == "portal.audio.done":
                response["sent_done"] = True
                logger.info("voice_response_completed conversation_id=%s response_id=%s input_item_id=%s phase=%s samples=%s",
                    cid, rid, response["input"], body["phase"], response["sent"])

    async def tool_worker():
        nonlocal executing
        while True:
            batch = await calls.get()
            executing = True
            item_id = batch.input_id
            decisions = []
            for payload in batch.calls:
                try:
                    decision = coordinator.runtime.dispatcher.resolve(payload["name"], payload["arguments"],
                        session.registered_tools, False, session.tool_catalog)
                except DomainError as exc:
                    await store.record(cid, epoch, "native_tool_error", payload["call_id"], {
                        "name": payload["name"], "input_item_id": item_id, "reason_code": exc.code,
                        "stage": "selection" if exc.code == "VOICE_TOOL_UNKNOWN" else "arguments"})
                    raise
                decisions.append(decision)
            versions = {name: await store.tool_revision(name)
                        for decision in decisions for name in decision.tool.required_tools}
            try:
                request = await asyncio.wait_for(asyncio.shield(input_future(item_id)), 5)
            except TimeoutError as exc:
                raise DomainError("VOICE_INPUT_INCOMPLETE", "The voice request did not finish transcribing.", 502, True) from exc
            request = request.strip()
            if not request or len(request) > 2000:
                raise DomainError("VOICE_INPUT_INCOMPLETE", "The completed voice request is invalid.", 502)
            await current()
            search = searches.setdefault(item_id, NativeInputSearch(gateway.settings.voice_max_retrievals_per_input))
            if batch.after_terminal:
                call_id = batch.calls[0]["call_id"]
                await store.record(cid, epoch, "native_search_limit", call_id, {
                    "input_item_id": item_id, "reason_code": "VOICE_TOOL_LOOP"})
                logger.warning("voice_search_post_terminal conversation_id=%s input_item_id=%s call_id=%s",
                               cid, item_id, call_id)
                raise DomainError("VOICE_TOOL_LOOP", "A new tool batch arrived after the final result.", 502, True)
            # Reserve bounded space for per-call dispositions before admitting
            # evidence. The model sees one result per batch; exact excerpts
            # already delivered in this input remain in its native history.
            outcomes = [{"index": str(i), "status": "not_executed", "reason": "search_finished"}
                        for i in range(batch.size)]
            search.result_overhead = len(json.dumps({"call_results": outcomes}).encode()) if batch.size > 1 else 0
            turn, bundle = last_results.get(item_id, (None, None))
            executed = []
            for index, (payload, decision) in enumerate(zip(batch.calls, decisions, strict=True)):
                call_id = payload["call_id"]
                await store.record(cid, epoch, "native_tool_arguments", call_id, {
                    "name": payload["name"], "arguments": decision.arguments.model_dump(),
                    "input_item_id": item_id, "matches_final_transcript": request == decision.arguments.query})
                if search.terminal_reason:
                    if batch.id is None:
                        raise DomainError("VOICE_TOOL_BATCH_REQUIRED", "Upgrade VoiceChat to settle queued tool batches.", 502, True)
                    # This member was received before a final result was sent.
                    # Settle it in this batch without starting a new Turn/query.
                    await store.record(cid, epoch, "native_batch_call_settled", call_id, {
                        "input_item_id": item_id, "batch_id": batch.id, "status": "not_executed",
                        "reason": "search_finished"})
                    logger.info("voice_batch_call_settled conversation_id=%s input_item_id=%s call_id=%s batch_id=%s",
                                cid, item_id, call_id, batch.id)
                    continue
                execute_search = search.reserve(decision.tool.name, decision.arguments.model_dump())
                outcomes[index] = {"index": str(index), "status": "executed" if execute_search else "reused"}
                logger.info("voice_bridge_started conversation_id=%s call_id=%s", cid, call_id)
                turn, task = await coordinator.submit(session.principal, cid, f"voice:{epoch}:{call_id}",
                    request, "voice", epoch, call_id, item_id, decision, voice_relay=True,
                    native_search=search, execute_search=execute_search)
                session.request_revision = turn.request_revision
                tool_inputs[item_id] = {"turn": turn.id, "call": call_id, "sent": False,
                                       "acks": decision.tool.ack_messages, "evidence": False}
                bundle = await task if task else None
                if bundle is None:
                    raise DomainError("VOICE_TOOL_RESULT_UNAVAILABLE", "The tool result is no longer available.", 502, True)
                last_results[item_id] = (turn, bundle)
                executed.append((call_id, turn.id))
            if turn is None or bundle is None:
                raise DomainError("VOICE_TOOL_RESULT_UNAVAILABLE", "The batch has no result to deliver.", 502, True)
            if isinstance(bundle, EvidenceReady):
                result = search.voice_result(bundle)
                tool_inputs[item_id]["evidence"] = True
            else:
                result = {"status": bundle.status, "speech_text": bundle.speech_text,
                          "search_state": "finished", "instructions": ANSWER_INSTRUCTIONS + FINAL_INSTRUCTIONS}
            if batch.size > 1:
                result["call_results"] = outcomes
            text = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
            if len(text.encode()) > gateway.settings.qa_direct_evidence_max_bytes:
                raise DomainError("VOICE_EVIDENCE_BUDGET", "The aggregate tool result exceeds its byte limit.", 502, True)
            call_ids = [p["call_id"] for p in batch.calls]
            async with coordinator.lock(cid):
                await current()
                if not await store.current(cid, epoch, turn.id, turn.request_revision):
                    raise DomainError("STALE_REVISION", "The tool request changed before delivery.", 409)
                allowed = await coordinator.runtime.registry.allowed(session.principal)
                for decision in decisions:
                    if not decision.tool.required_tools <= allowed or decision.tool.permission_scope not in session.principal.scopes:
                        raise DomainError("FORBIDDEN", "Tool access changed before delivery.", 403)
                if any([await store.tool_revision(name) != version
                        for name, version in {**versions, **search.versions}.items()]):
                    raise DomainError("FORBIDDEN", "Tool configuration changed before delivery.", 403)
                snapshot = await coordinator.runtime.catalog.snapshot(False)
                if snapshot.revision != session.catalog_revision or snapshot.definitions != session.tool_definitions:
                    raise DomainError("VOICE_TOOL_CONFIGURATION_CHANGED", "Voice tools changed. Reconnect.", 409, True)
                for call_id in call_ids:
                    await store.delivery(cid, epoch, call_id, "tool_result", "write_started", turn.id, turn.request_revision,
                        input_item_id=item_id)
                tool_inputs[item_id]["sent"] = True
                async with upstream_lock:
                    async with asyncio.timeout(2):
                        if batch.id:
                            await provider.submit_tool_result(call_ids[0], text, batch_id=batch.id, call_ids=call_ids)
                        else:
                            await provider.submit_tool_result(call_ids[0], text)
                search.remember_delivery(result)
                if result.get("search_state") == "finished":
                    terminal_sent.add(item_id)
                for call_id in call_ids:
                    await store.delivery(cid, epoch, call_id, "tool_result", "sent", turn.id, turn.request_revision)
                    logger.info("voice_tool_result_submitted conversation_id=%s turn_id=%s call_id=%s bytes=%s",
                                cid, turn.id, call_id, len(text.encode()))
            counters = {"input_item_id": item_id, "retrieval_executions": search.executions,
                        "retrieval_limit": search.limit, "duplicate_queries": search.duplicate_queries,
                        "terminal_result_submitted": result.get("search_state") == "finished",
                        "terminal_reason": search.terminal_reason,
                        "cumulative_sources": len(bundle.citations) if isinstance(bundle, EvidenceReady) else 0}
            await store.record(cid, epoch, "native_search_state", call_ids[0], counters, turn.id, turn.request_revision)
            await store.record(cid, epoch, "native_tool_batch", batch.id or call_ids[0], {
                "input_item_id": item_id, "call_ids": call_ids, "outcomes": outcomes,
                "result_injections": 1, "new_sources": len(result.get("sources", [])),
                "payload_bytes": len(text.encode()),
                "retained_sources": len(search.delivered_sources)}, turn.id, turn.request_revision)
            logger.info("voice_search_state conversation_id=%s input_item_id=%s call_id=%s "
                        "retrieval_executions=%s retrieval_limit=%s duplicate_queries=%s "
                        "terminal_result_submitted=%s cumulative_sources=%s",
                        cid, item_id, call_ids[0], search.executions, search.limit, search.duplicate_queries,
                        int(counters["terminal_result_submitted"]), counters["cumulative_sources"])
            logger.info("voice_batch_result_submitted conversation_id=%s input_item_id=%s batch_id=%s calls=%s injections=1",
                        cid, item_id, batch.id, len(call_ids))
            for call_id, turn_id in executed:
                logger.info("voice_bridge_finished conversation_id=%s turn_id=%s call_id=%s status=result_sent",
                            cid, turn_id, call_id)
            executing = False

    async def receiver():
        nonlocal latest_input, input_state
        async for event in provider.events():
            await current()
            body = dict(event.payload)
            if event.kind == "session.ended":
                raise DomainError("VOICE_UNAVAILABLE", "VoiceChat ended the connection. Restart voice.", 503, True)
            if event.kind == "input.state":
                input_state = body["state"]
                if input_state == "speaking":
                    latest_input = body["item_id"]
                    input_future(latest_input)
                    logger.info("voice_input_started conversation_id=%s input_item_id=%s", cid, latest_input)
                    # Clear already-buffered playback on native speech start;
                    # continue receiving and forwarding the provider's stream.
                    emit("playback.clear", {"message": "Customer speaking"})
                emit("input.state", {"state": input_state})
                continue
            if event.kind.startswith("transcript"):
                item_id = body["item_id"]
                if item_id not in inputs:
                    latest_input = item_id
                future = input_future(item_id)
                if event.kind == "transcript.done":
                    logger.info("voice_input_transcribed conversation_id=%s input_item_id=%s text_chars=%s text_sha256=%s",
                                cid, item_id, len(body["text"]), hashlib.sha256(body["text"].encode()).hexdigest())
                    await store.utterance(session.principal, cid, epoch, item_id, text=body["text"])
                    if not future.done():
                        future.set_result(body["text"])
                    await store.record(cid, epoch, "user_transcript", item_id, body)
                emit(event.kind, body)
                continue
            if event.kind == "tool":
                call_id = body["call_id"]
                # Only identical notifications of the same native call are
                # deduplicated. New IDs/queries remain actual model requests.
                fingerprint = (body["name"], body["arguments"], body.get("native_batch_id"),
                               body.get("native_batch_size"), body.get("output_index"))
                if call_id in seen_calls:
                    if seen_calls[call_id] != fingerprint:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "A tool call ID was reused with different arguments.", 502)
                    continue
                if latest_input is None:
                    raise DomainError("VOICE_INPUT_INCOMPLETE", "The tool call has no customer input.", 502)
                if len(seen_calls) >= 128:
                    raise DomainError("VOICE_TOOL_LIMIT", "Too many tool calls in this connection.", 502, True)
                seen_calls[call_id] = fingerprint
                try:
                    batch = batches.append(body, latest_input, latest_input in terminal_sent)
                    if batch is not None:
                        calls.put_nowait(batch)
                except asyncio.QueueFull as exc:
                    raise DomainError("VOICE_TOOL_LIMIT", "Too many pending tool calls.", 502, True) from exc
                logger.info("voice_tool_call_received conversation_id=%s call_id=%s tool=%s input_item_id=%s",
                    cid, call_id, body["name"], latest_input)
                # ACK metadata is available before the worker starts.
                rid = body.get("response_id")
                if rid:
                    response = response_for(rid)
                    tool = session.tool_catalog.get(body["name"])
                    response["acks"] = tool.ack_messages if tool else ()
                continue
            if not event.kind.startswith(("audio.", "speech_text.")):
                continue
            rid = body["response_id"]
            response = response_for(rid)
            if event.kind == "audio.delta" and not response.get("audio_received"):
                response["audio_received"] = True
                logger.info("voice_audio_first_received conversation_id=%s response_id=%s input_item_id=%s",
                    cid, rid, response["input"])
            operation = tool_inputs.get(response["input"])
            if session.suppress_next_response:
                session.suppressed_responses.add(rid)
                session.suppress_next_response = False
            if event.kind.startswith("speech_text"):
                acks = response["acks"] or (operation["acks"] if operation else ())
                if event.kind == "speech_text.delta":
                    response["stream_text"] += body["text"]
                    normalized = " ".join(response["stream_text"].split())
                    # Withhold a possible fixed ACK prefix, not arbitrary speech.
                    # If it diverges, release its complete buffered text once.
                    response["ack"] = any(ack.startswith(normalized) for ack in acks)
                    if not response["ack"]:
                        body["text"] = response["stream_text"][response["streamed_chars"]:]
                        response["streamed_chars"] = len(response["stream_text"])
                else:
                    response["text"] = body["text"]
                    response["ack"] = " ".join(body["text"].split()) in acks
                    if not response["ack"]:
                        await store.record(cid, epoch, "voicechat_transcript", rid, {
                            **body, "input_item_id": response["input"],
                            "_authorized_kb_ids": sorted(session.principal.knowledge_base_ids) if operation else []})
                body.update(phase="status" if response["ack"] else "answer", segment_index=0,
                    input_item_id=response["input"], answer_kind="knowledge" if operation else "general")
            if event.kind == "audio.done":
                body["phase"] = "status" if response["ack"] else "answer"
                response["done"] = True
                if response["text"] and not response["ack"]:
                    if operation and operation["sent"] and operation["evidence"]:
                        coordinator.provider_answer(operation["turn"], response["text"], response_id=rid)
                    elif not operation and response["input"]:
                        await store.utterance(session.principal, cid, epoch, response["input"], answer=AnswerBundle(
                            status="answered", display_text=response["text"], speech_text="", answer_kind="general",
                            composition="provider_general", validation_level="provider_only", verification_timing="not_verified",
                            is_mock=gateway.settings.voice_provider == "mock"))
            emit(event.kind, body)
        raise DomainError("VOICE_UNAVAILABLE", "VoiceChat disconnected. Restart voice.", 503, True)

    def response_for(rid):
        if rid not in responses:
            if len(responses) >= 1024:
                raise DomainError("VOICE_SESSION_ROTATION_REQUIRED", "Reconnect to continue the conversation.", 409, True)
            responses[rid] = {"input": latest_input, "text": "", "ack": False, "acks": (),
                              "sent": 0, "done": False, "sent_done": False, "streamed_chars": 0, "stream_text": ""}
        return responses[rid]

    async def browser():
        nonlocal client_seq, frames, last_input
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
                if frames * .08 - (last_input - started) > .5:
                    raise DomainError("AUDIO_BACKPRESSURE", "Audio was sent too quickly.", 409)
                async with upstream_lock:
                    async with asyncio.timeout(2):
                        await provider.send_audio(event.payload.audio)
            elif isinstance(event, PortalPlaybackStop):
                await gateway.suppress_playback(cid, epoch, event.payload.response_id)
                emit("playback.clear", {"message": "Playback stopped", "response_id": event.payload.response_id})
            elif isinstance(event, PortalPlaybackAck):
                response = responses.get(event.payload.response_id)
                if not response or event.payload.played_samples > response["sent"]:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Playback acknowledgement exceeds sent audio.", 400)
                if event.payload.finished and not response["sent_done"]:
                    raise DomainError("VOICE_PROTOCOL_ERROR", "Playback finished before audio delivery.", 400)
            elif isinstance(event, PortalSessionClose) or event.type == "portal.interrupt":
                return

    async def watchdog():
        while True:
            await asyncio.sleep(1)
            await current()
            snapshot = await coordinator.runtime.catalog.snapshot(False)
            if snapshot.revision != session.catalog_revision or snapshot.definitions != session.tool_definitions:
                raise DomainError("VOICE_TOOL_CONFIGURATION_CHANGED", "Voice tools changed. Reconnect.", 409, True)
            batches.check_timeout()
            if time.monotonic() - last_input > 5:
                raise DomainError("AUDIO_BACKPRESSURE", "Audio capture stalled. Restart voice.", 409, True)
            elapsed = time.monotonic() - started
            if elapsed > gateway.settings.voice_session_max_seconds:
                # A finished ACK and an empty tool queue do not mean the
                # model has finished answering. Reuse the existing observer;
                # this controls idle rotation only, never permission to speak.
                awaiting_answer = any(
                    operation["turn"] in coordinator.awaiting_answers
                    for operation in tool_inputs.values()
                )
                if (input_state == "quiet" and not executing and calls.empty() and not batches.pending and not awaiting_answer
                    and outgoing.empty() and all(r["sent_done"] for r in responses.values())):
                    raise DomainError("VOICE_SESSION_ROTATION_REQUIRED", "Reconnect to continue the conversation.", 409, True)
                if elapsed > gateway.settings.voice_session_max_seconds + 30:
                    raise DomainError("VOICE_SESSION_EXPIRED", "Voice connection reached its time limit. Restart voice.", 409, True)

    try:
        await current()
        await provider.connect(session.summary)
        emit("session.ready", {"sample_rate": 24000, "format": "pcm16", "chunk_ms": 80,
            "is_mock": gateway.settings.voice_provider == "mock"})
        tasks = [asyncio.create_task(fn()) for fn in (writer, receiver, browser, tool_worker, watchdog)]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except DomainError as exc:
        error = exc
    except (asyncio.CancelledError, WebSocketDisconnect):
        pass
    except Exception:
        logger.exception("voice_relay_failed conversation_id=%s", cid)
        error = DomainError("VOICE_UNAVAILABLE", "Voice connection ended. Restart voice.", 503, True)
    finally:
        session.closing = True
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if error:
                logger.warning("voice_session_failed conversation_id=%s reason_code=%s", cid, error.code)
                try:
                    async with asyncio.timeout(1):
                        seq += 1
                        await ws.send_json(PortalEvent(type="portal.error", conversation_id=cid, epoch=epoch,
                            request_revision=session.request_revision, server_seq=seq, payload=error.payload()).model_dump(mode="json"))
                except Exception:
                    pass
            confirmed = False
            try:
                async with asyncio.timeout(3):
                    confirmed = await provider.close() is True
            except Exception:
                pass
            await store.settle_deliveries(cid, epoch, "connection_closed")
            await store.finish_voice_session(cid, epoch, session.id, confirmed)
            gateway.sessions.pop(session.id, None)
            try:
                await coordinator.interrupt(session.principal, cid, epoch, "connection_closed")
            except DomainError:
                pass
            try:
                await ws.close()
            except Exception:
                pass
