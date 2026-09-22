import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass

import anyio
from app.contracts import (
    BridgeArguments,
    DomainError,
    PortalAudioAppend,
    PortalEvent,
    PortalPlaybackAck,
    PortalPlaybackStop,
    PortalSessionClose,
    portal_client_event_adapter,
    portal_server_event_adapter,
    printable_ascii,
    uid,
)
from app.voice.provider import BRIDGE_NAME, MockVoiceAdapter, NvidiaVoiceChatAdapter


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
                or (
                    s.app_env != "integration"
                    and (not s.voicechat_integration_verified or not s.voicechat_api_version)
                )
            ):
                raise DomainError("VOICE_UNAVAILABLE", "Real voice integration has not been verified and is unavailable", 503)
            if len(self.sessions) >= s.max_voice_sessions:
                raise DomainError("VOICE_CAPACITY_EXCEEDED", "Voice capacity is full. Use text or try again later.", 429, True)
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
                summary = "\n".join(
                    str(item.get("content", "")) for item in visible_history[-6:]
                )[-1500:]
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
                )
            self.sessions[sid] = session
            return {
                "voice_session_id": sid,
                "epoch": epoch,
                "request_revision": request_revision,
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
        incoming = asyncio.Queue(maxsize=6)  # 480 ms at fixed 80 ms chunks.
        controls = asyncio.Queue(maxsize=16)
        outgoing = asyncio.Queue(maxsize=12)
        wake = asyncio.Event()
        pending, workers, sent_samples = {}, set(), {}
        seq, client_seq, started = 0, -1, time.monotonic()
        frames, last_input, last_ack, last_playback_stop = 0, started, 0.0, 0.0
        input_state = "quiet"

        async def current(tid=None, revision=None):
            await self.coordinator.coordination.check(session.conversation_id)
            return not session.stopped and await self.store.current(
                session.conversation_id, session.epoch, tid, revision
            )

        def emit(kind, payload):
            nonlocal seq
            seq += 1
            e = PortalEvent(
                type="portal." + kind,
                conversation_id=session.conversation_id,
                epoch=session.epoch,
                request_revision=session.request_revision,
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
                if await current():
                    async with asyncio.timeout(2):
                        await ws.send_json(event)

        async def upstream_writer():
            while True:
                await wake.wait()
                wake.clear()
                while not controls.empty() or not incoming.empty():
                    if not await current():
                        return
                    if not controls.empty():
                        call, tid, revision, text = controls.get_nowait()
                        # This is the final fence immediately at the single writer.
                        async with self.coordinator.lock(session.conversation_id):
                            if await current(tid, revision) and pending.get(call) == "ready":
                                await provider.submit_tool_result(call, text)
                                pending[call] = "sent"
                    else:
                        audio = incoming.get_nowait()
                        await provider.send_audio(audio)

        async def bridge(payload):
            call_id = payload["call_id"]
            tid = None
            revision = session.request_revision
            try:
                if (
                    payload["name"] != BRIDGE_NAME
                    or not isinstance(payload["arguments"], str)
                    or len(payload["arguments"]) > 12000
                ):
                    raise ValueError("Unsupported bridge")
                args = BridgeArguments.model_validate_json(payload["arguments"])
                turn, task = await self.coordinator.submit(
                    session.principal,
                    session.conversation_id,
                    f"voice:{session.epoch}:{call_id}",
                    args.user_request,
                    "voice",
                    session.epoch,
                    call_id,
                )
                tid = turn.id
                revision = turn.request_revision
                session.request_revision = revision
                bundle = await task if task else None
                if not bundle or not await current(tid, revision):
                    return
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
                        },
                        ensure_ascii=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                text = json.dumps(
                    {"status": "failed", "speech_text": "The request failed. Please ask again.", "language": "en-US"}, ensure_ascii=True
                )
            if await current(tid, revision):
                pending[call_id] = "ready"
                controls.put_nowait((call_id, tid, revision, text))
                wake.set()

        async def receiver():
            nonlocal input_state
            async for event in provider.events():
                if not await current():
                    return
                if event.kind == "tool":
                    call = event.payload["call_id"]
                    if call in pending:
                        continue
                    if len(pending) >= 128:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Too many voice tool calls", 502)
                    if any(v in ("running", "ready") for v in pending.values()):
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Parallel cloud tool calls are unsupported. Please ask again.", 502)
                    pending[call] = "running"
                    task = asyncio.create_task(bridge(event.payload))
                    workers.add(task)
                    task.add_done_callback(workers.discard)
                    continue
                if event.kind == "session.ended":
                    return
                if event.kind == "input.state":
                    input_state = event.payload["state"]
                response_id = event.payload.get("response_id")
                if response_id and session.suppress_next_response:
                    session.suppressed_responses.add(response_id)
                suppressed = bool(
                    response_id and response_id in (session.suppressed_responses or set())
                )
                if event.kind.endswith(".done") and "text" in event.payload and not suppressed:
                    kind = "voicechat_transcript" if event.kind.startswith("speech") else "user_transcript"
                    source = event.payload.get("item_id") or event.payload["response_id"]
                    stored_payload = dict(event.payload)
                    if kind == "voicechat_transcript":
                        stored_payload["_authorized_kb_ids"] = sorted(
                            session.principal.knowledge_base_ids
                        )
                    if not await self.store.record(
                        session.conversation_id, session.epoch, kind, source, stored_payload
                    ):
                        continue
                if event.kind == "audio.delta":
                    audio = base64.b64decode(event.payload["audio"], validate=True)
                    if len(audio) % 2 or len(audio) > 48000:
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Voice audio frame is not PCM16", 502)
                    response_id = event.payload["response_id"]
                    sent_samples[response_id] = sent_samples.get(response_id, 0) + len(audio) // 2
                if suppressed:
                    if event.kind == "audio.done":
                        session.suppressed_responses.discard(response_id)
                        session.suppress_next_response = False
                    continue
                emit(event.kind, event.payload)

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
                    if samples > sent_samples.get(response_id, -1):
                        raise DomainError("VOICE_PROTOCOL_ERROR", "Playback acknowledgement exceeds sent audio", 400)
                    if time.monotonic() - last_ack > 0.5:
                        await self.store.record(
                            session.conversation_id,
                            session.epoch,
                            "playback_ack",
                            response_id,
                            {"response_id": response_id, "played_samples": samples, "estimated": True},
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
                if session.principal.expires_at is not None and session.principal.expires_at <= time.time():
                    raise DomainError("AUTH_REQUIRED", "Your session expired. Please sign in again.", 401)
                if not await current():
                    return
                if time.monotonic() - last_input > 5:
                    raise DomainError("AUDIO_BACKPRESSURE", "Audio capture stalled. Restart voice.", 409, True)
                elapsed = time.monotonic() - started
                if elapsed > self.settings.voice_session_max_seconds and input_state == "quiet" and not any(
                    value in ("running", "ready") for value in pending.values()
                ):
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
                await provider.close()
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
                            session.principal, session.conversation_id, session.epoch
                        )
                    except DomainError:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
