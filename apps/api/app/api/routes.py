import asyncio
import json
from typing import Annotated

from app.api.auth import principal
from app.contracts import (
    ConversationInput,
    DomainError,
    InterruptInput,
    MessageInput,
    Principal,
    StopPlaybackInput,
    StrictModel,
    TaskControlInput,
    now,
)
from app.storage.models import AdminAudit, Conversation, Event, Record, ToolConfig, ToolRun, Turn
from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import select

router = APIRouter(prefix="/api/v1")
User = Annotated[Principal, Depends(principal)]


def _configured_kbs(settings):
    return {value.strip() for value in settings.knowledge_base_ids.split(",") if value.strip()}


def _answer_for_principal(answer, user, settings):
    if not answer:
        return answer
    current = set(user.knowledge_base_ids)
    configured = _configured_kbs(settings)
    citations = []
    for citation in answer.get("citations", []):
        scope = citation.get("authorized_kb_ids")
        # Old rows predate authorization scope tagging. Customers never receive
        # them; privileged roles need the complete current deployment scope.
        if (scope is None and ("customer" in user.roles or current != configured)) or (
            scope is not None and not set(scope).issubset(current)
        ):
            return {
                **answer,
                "status": "failed",
                "display_text": "Knowledge access changed. Please ask again.",
                "speech_text": "",
                "citations": [],
                "cards": [],
                "reason_code": "KB_ACCESS_REVOKED",
            }
        # JSON answers saved before D03 used version/updated_at and did not
        # contain CueKB location fields. Normalize them only at the read edge;
        # never invent a new document timestamp or rewrite persisted history.
        citations.append(
            {
                **citation,
                "version_id": citation.get("version_id") or citation.get("version"),
                "business_version": citation.get("business_version"),
                "updated_at": citation.get("updated_at"),
                "context": citation.get("context"),
                "context_parts": citation.get("context_parts", []),
                "context_truncated": citation.get("context_truncated", False),
                "context_omitted": citation.get("context_omitted", False),
                "hits_omitted": citation.get("hits_omitted", 0),
                "relations": citation.get("relations", []),
                "trace_id": citation.get("trace_id") or citation.get("retrieval_id", "legacy"),
                "retrieval_status": citation.get("retrieval_status", "ok"),
                "evidence_status": citation.get("evidence_status", "unassessed"),
                "degraded_reasons": citation.get("degraded_reasons", []),
                "scope_limited": citation.get("scope_limited", False),
                "content_revisions": citation.get("content_revisions", {}),
                "rank": citation.get("rank", 1),
                "title_path": citation.get("title_path", []),
                "anchor": citation.get("anchor", {}),
                "retrieval_sources": citation.get("retrieval_sources", []),
                "metadata": citation.get("metadata", {}),
            }
        )
    return {**answer, "citations": citations}


def _event_for_principal(payload, user, settings):
    if payload.get("type") != "portal.answer.final":
        return payload
    return {
        **payload,
        "payload": _answer_for_principal(payload.get("payload"), user, settings),
    }


def _record_for_principal(record, user, settings):
    payload = dict(record.payload)
    scope = payload.pop("_authorized_kb_ids", None)
    if record.kind == "voicechat_transcript":
        current = set(user.knowledge_base_ids)
        configured = _configured_kbs(settings)
        if (scope is None and ("customer" in user.roles or current != configured)) or (
            scope is not None and not set(scope).issubset(current)
        ):
            return None
    return {
        "kind": record.kind,
        "epoch": record.epoch,
        "source_id": record.source_id,
        "payload": payload,
    }


def capabilities(s):
    voice_configured = bool(
        s.voice_provider == "mock"
        or (s.voice_provider == "nvidia" and s.voicechat_ws_url and s.voicechat_api_key.get_secret_value())
    )
    return {
        "provider": s.voice_provider,
        "is_mock": s.mock,
        "agent_provider": s.agent_provider,
        "cuekb_mode": s.cuekb_mode,
        "weather_mode": s.weather_mode,
        "enabled_tools": sorted(s.enabled_tool_names),
        "voice_available": voice_configured,
        "text_configured": s.agent_provider == "mock"
        or bool(s.agent_model and s.openai_api_key.get_secret_value()),
        "streaming_audio": s.voice_provider in ("mock", "nvidia"),
        "native_full_duplex": voice_configured,
        "function_result_return": voice_configured,
        "native_cancel_response": False,
        "native_tool_phase_barge_in": voice_configured,
        "dynamic_instructions": False,
        "arbitrary_text_to_speech": False,
        "required_voice_languages": ["en-US"],
        "declared_voice_languages": ["en-US"],
        "integration_verified_voice_languages": [],
        "tool_phase_recovery": "close_and_reconnect",
        "voice_session_max_seconds": s.voice_session_max_seconds,
    }


@router.get("/capabilities")
async def get_capabilities(request: Request, user: User):
    return {
        **capabilities(request.app.state.settings),
        "available_tools": sorted(await request.app.state.registry.allowed(user)),
    }


async def limited(request, user):
    await request.app.state.coordination.check()
    await request.app.state.coordination.rate_limit(
        f"{user.tenant_id}:{user.user_id}", request.app.state.settings.request_limit_per_minute
    )


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationInput, request: Request, user: User):
    await limited(request, user)
    async with request.app.state.store.transaction() as db:
        c = Conversation(tenant_id=user.tenant_id, user_id=user.user_id, title=body.title, locale=body.locale)
        db.add(c)
        await db.flush()
        return {
            "id": c.id,
            "title": c.title,
            "epoch": c.epoch,
            "request_revision": c.request_revision,
            "locale": c.locale,
        }


@router.get("/conversations")
async def conversations(
    request: Request, user: User, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)
):
    async with request.app.state.store.sessions() as db:
        rows = (
            await db.execute(
                select(Conversation)
                .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.user_id)
                .order_by(Conversation.updated_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
        return {
            "items": [
                {
                    "id": c.id,
                    "title": c.title,
                    "epoch": c.epoch,
                    "request_revision": c.request_revision,
                    "locale": c.locale,
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in rows
            ]
        }


@router.get("/conversations/{cid}/messages")
async def messages(
    cid: str, request: Request, user: User, before: str | None = None, limit: int = Query(30, ge=1, le=100)
):
    store = request.app.state.store
    async with store.sessions() as db:
        c = await store.get(db, cid, user)
        q = select(Turn).where(Turn.conversation_id == cid)
        if before:
            previous = await db.get(Turn, before)
            if not previous or previous.conversation_id != cid:
                raise DomainError("FORBIDDEN", "Invalid history cursor", 404)
            q = q.where(Turn.created_at < previous.created_at)
        turns = list((await db.execute(q.order_by(Turn.created_at.desc()).limit(limit))).scalars())
        records = list(
            (
                await db.execute(
                    select(Record)
                    .where(Record.conversation_id == cid)
                    .order_by(Record.created_at.desc())
                    .limit(100)
                )
            ).scalars()
        )
        return {
            "epoch": c.epoch,
            "request_revision": c.request_revision,
            "voice_session_id": c.voice_session_id,
            "items": [
                {
                    "id": t.id,
                    "epoch": t.epoch,
                    "request_revision": t.request_revision,
                    "parent_task_id": t.parent_task_id,
                    "native_call_id": t.native_call_id,
                    "user_text": t.user_text,
                    "channel": t.channel,
                    "status": t.status,
                    "cancellation_reason": t.cancellation_reason,
                    "delivery_status": t.delivery_status,
                    "output_suppressed": t.output_suppressed,
                    "answer": _answer_for_principal(t.answer, user, request.app.state.settings),
                    "created_at": t.created_at.isoformat(),
                }
                for t in reversed(turns)
            ],
            "records": [
                visible
                for r in reversed(records)
                if (visible := _record_for_principal(r, user, request.app.state.settings))
                is not None
            ],
            "next_before": turns[-1].id if len(turns) == limit else None,
        }


@router.post("/conversations/{cid}/messages", status_code=202)
async def send_message(
    cid: str,
    body: MessageInput,
    request: Request,
    user: User,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    await limited(request, user)
    turn, _ = await request.app.state.coordinator.submit(user, cid, "text:" + idempotency_key, body.text)
    return {
        "turn_id": turn.id,
        "task_id": turn.id,
        "epoch": turn.epoch,
        "request_revision": turn.request_revision,
        "status": turn.status,
    }


@router.get("/conversations/{cid}/events")
async def events(
    cid: str,
    request: Request,
    user: User,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
):
    store = request.app.state.store
    async with store.sessions() as db:
        await store.get(db, cid, user)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError as exc:
        raise DomainError("INVALID_CURSOR", "Invalid event cursor", 422) from exc

    async def generate():
        nonlocal cursor
        checked = 0
        while not await request.is_disconnected():
            if checked % 100 == 0:
                try:
                    await principal(request)
                except DomainError:
                    return
            checked += 1
            async with store.sessions() as db:
                await store.get(db, cid, user)
                rows = (
                    (
                        await db.execute(
                            select(Event)
                            .where(Event.conversation_id == cid, Event.server_seq > cursor)
                            .order_by(Event.server_seq)
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )
            for e in rows:
                cursor = e.server_seq
                safe_payload = _event_for_principal(
                    e.payload, user, request.app.state.settings
                )
                yield f"id: {cursor}\ndata: {json.dumps(safe_payload, ensure_ascii=False)}\n\n"
            if not rows and checked % 30 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{cid}/interrupt")
async def interrupt(cid: str, body: InterruptInput, request: Request, user: User):
    async with request.app.state.store.sessions() as db:
        await request.app.state.store.get(db, cid, user)
    epoch = await request.app.state.coordinator.interrupt(user, cid, body.expected_epoch)
    return {"epoch": epoch, "status": "stopped"}


@router.post("/conversations/{cid}/tasks/current/cancel")
async def cancel_current_task(cid: str, body: TaskControlInput, request: Request, user: User):
    await limited(request, user)
    revision, changed = await request.app.state.coordinator.cancel_task(
        user, cid, body.expected_epoch, body.expected_revision
    )
    return {
        "request_revision": revision,
        "status": "canceled" if changed else "already_finished",
    }


@router.post("/conversations/{cid}/playback/stop")
async def stop_playback(cid: str, body: StopPlaybackInput, request: Request, user: User):
    await limited(request, user)
    revision = await request.app.state.coordinator.stop_playback(
        user,
        cid,
        body.expected_epoch,
        body.expected_revision,
        body.response_id,
    )
    return {"request_revision": revision, "status": "playback_stopped"}


@router.post("/conversations/{cid}/voice-sessions", status_code=201)
async def voice_session(cid: str, request: Request, user: User):
    await limited(request, user)
    return await request.app.state.voice.issue(user, cid)


@router.delete("/conversations/{cid}/voice-sessions/{sid}")
async def end_voice(cid: str, sid: str, request: Request, user: User):
    async with request.app.state.store.sessions() as db:
        c = await request.app.state.store.get(db, cid, user)
        expected = c.epoch
        if c.voice_session_id != sid:
            return {"status": "already_closed", "epoch": expected}
    return {"status": "closed", "epoch": await request.app.state.coordinator.interrupt(user, cid, expected)}


@router.websocket("/voice-sessions/{sid}/stream")
async def voice_stream(ws: WebSocket, sid: str, ticket: str = ""):
    await ws.app.state.voice.stream(ws, sid, ticket)


def admin(user):
    if "tools:admin" not in user.scopes:
        raise DomainError("FORBIDDEN", "Tool administration permission is required", 403)


@router.get("/admin/tools")
async def tool_list(request: Request, user: User):
    admin(user)
    registry = request.app.state.registry
    async with registry.store.sessions() as db:
        errors = (
            await db.execute(
                select(ToolRun).where(ToolRun.status != "ok").order_by(ToolRun.created_at.desc()).limit(20)
            )
        ).scalars()
        recent = [
            {"name": e.name, "code": e.status, "duration_ms": e.duration_ms, "at": e.created_at.isoformat()}
            for e in errors
        ]
    return {
        "version": registry.version,
        "items": [
            {
                "name": s.name,
                "description": s.description,
                "display_name": s.display_name,
                "version": s.version,
                "deployment_enabled": registry.deployment_enabled(s.name),
                "enabled": await registry.available(s.name),
                "read_only": s.read_only,
                "timeout_ms": s.timeout_ms,
            }
            for s in registry.specs.values()
        ],
        "recent_errors": recent,
    }


class ToolPatch(StrictModel):
    enabled: bool


@router.patch("/admin/tools/{name}")
async def toggle_tool(name: str, body: ToolPatch, request: Request, user: User):
    admin(user)
    registry = request.app.state.registry
    if name not in registry.specs:
        raise DomainError("TOOL_NOT_FOUND", "Tool not found", 404)
    if body.enabled and not registry.deployment_enabled(name):
        raise DomainError("TOOL_NOT_DEPLOYED", "This tool is not enabled in this deployment", 409)
    async with request.app.state.store.transaction() as db:
        config = await db.get(ToolConfig, name)
        if config:
            config.enabled, config.revision, config.updated_at = body.enabled, config.revision + 1, now()
        else:
            db.add(ToolConfig(name=name, enabled=body.enabled))
        db.add(
            AdminAudit(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                action="tool.enabled",
                details={"name": name, "enabled": body.enabled},
            )
        )
    return {"name": name, "enabled": body.enabled}


@router.post("/admin/tools/{name}/test")
async def test_tool(name: str, request: Request, user: User):
    admin(user)
    await limited(request, user)
    registry, s = request.app.state.registry, request.app.state.settings
    spec = registry.specs.get(name)
    if not spec:
        raise DomainError("TOOL_NOT_FOUND", "Tool not found", 404)
    if not registry.deployment_enabled(name):
        raise DomainError("TOOL_NOT_DEPLOYED", "This tool is not enabled in this deployment", 409)
    if not await registry.store.enabled(name):
        raise DomainError("TOOL_DISABLED", "This tool was disabled by an administrator", 409)
    mode = getattr(s, spec.mode_ref)
    if mode == "mock":
        return {"status": "mock", "message": "Demo adapter; this does not verify a real service connection"}
    base = getattr(s, spec.endpoint_ref)
    if not base:
        raise DomainError("TOOL_NOT_CONFIGURED", "Tool endpoint is not configured", 503)
    try:
        response = await request.app.state.client.get(
            base.rstrip("/") + "/health",
            timeout=3,
            headers={"Authorization": "Bearer " + getattr(s, spec.secret_ref).get_secret_value()},
        )
        response.raise_for_status()
        return {"status": "reachable", "message": "Health endpoint is reachable; business search has not been accepted"}
    except Exception as exc:
        raise DomainError("TOOL_UNAVAILABLE", "Health endpoint is unreachable; check service configuration", 503, True) from exc


@router.get("/admin/services")
async def services(request: Request, user: User):
    admin(user)
    s = request.app.state.settings
    voice = "configured" if s.voice_provider == "nvidia" and s.voicechat_ws_url else s.voice_provider
    return {
        "voice_health": voice,
        "text_model": "mock"
        if s.agent_provider == "mock"
        else ("configured" if s.agent_model and s.openai_api_key.get_secret_value() else "unconfigured"),
        "text_note": "Configuration status does not verify real inference",
        "active_voice_sessions": len(request.app.state.voice.sessions),
        "voice_capacity_per_gateway": s.max_voice_sessions,
        "active_business_runs": len(request.app.state.coordinator.tasks),
        "draining": request.app.state.coordinator.draining,
    }


@router.post("/admin/drain")
async def drain(request: Request, user: User):
    admin(user)
    coordinator = request.app.state.coordinator
    coordinator.draining = True
    async with request.app.state.store.transaction() as db:
        db.add(AdminAudit(tenant_id=user.tenant_id, user_id=user.user_id, action="gateway.drain", details={}))
    return {
        "status": "draining",
        "message": "This replica no longer accepts new sessions. Active sessions will finish within their limits before shutdown.",
    }
