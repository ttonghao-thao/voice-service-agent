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
    StrictModel,
    now,
)
from app.storage.models import AdminAudit, Conversation, Event, Record, ToolConfig, ToolRun, Turn
from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import select

router = APIRouter(prefix="/api/v1")
User = Annotated[Principal, Depends(principal)]


def capabilities(s):
    return {
        "provider": s.voice_provider,
        "is_mock": s.mock,
        "agent_provider": s.agent_provider,
        "rag_mode": s.rag_mode,
        "weather_mode": s.weather_mode,
        "voice_available": s.voice_provider == "mock"
        or bool(
            s.voicechat_ws_url
            and (s.app_env == "integration" or (s.voicechat_integration_verified and s.voicechat_api_version))
        ),
        "text_configured": s.agent_provider == "mock"
        or bool(s.agent_model and s.openai_api_key.get_secret_value()),
        "native_full_duplex": s.voice_provider == "nvidia",
        "function_result_return": s.voice_provider == "nvidia",
        "native_cancel_response": False,
        "native_tool_phase_barge_in": False,
        "dynamic_instructions": False,
        "arbitrary_text_to_speech": False,
        "required_voice_languages": ["zh-CN"],
        "declared_voice_languages": ["zh-CN"],
        "integration_verified_voice_languages": ["zh-CN"]
        if s.voicechat_integration_verified and s.voicechat_api_version
        else [],
        "api_version": s.voicechat_api_version or None,
        "tool_phase_recovery": "close_and_reconnect",
        "voice_session_max_seconds": s.voice_session_max_seconds,
    }


@router.get("/capabilities")
async def get_capabilities(request: Request, user: User):
    return capabilities(request.app.state.settings)


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
        return {"id": c.id, "title": c.title, "epoch": c.epoch, "locale": c.locale}


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
                raise DomainError("FORBIDDEN", "历史游标无效", 404)
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
            "voice_session_id": c.voice_session_id,
            "items": [
                {
                    "id": t.id,
                    "epoch": t.epoch,
                    "user_text": t.user_text,
                    "channel": t.channel,
                    "status": t.status,
                    "answer": t.answer,
                    "created_at": t.created_at.isoformat(),
                }
                for t in reversed(turns)
            ],
            "records": [
                {"kind": r.kind, "epoch": r.epoch, "source_id": r.source_id, "payload": r.payload}
                for r in reversed(records)
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
    return {"turn_id": turn.id, "epoch": turn.epoch, "status": turn.status}


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
        raise DomainError("INVALID_CURSOR", "事件游标格式错误", 422) from exc

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
                yield f"id: {cursor}\ndata: {json.dumps(e.payload, ensure_ascii=False)}\n\n"
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
        raise DomainError("FORBIDDEN", "需要工具管理权限", 403)


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
                "enabled": await registry.store.enabled(s.name),
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
    if name not in request.app.state.registry.specs:
        raise DomainError("TOOL_NOT_FOUND", "工具不存在", 404)
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
        raise DomainError("TOOL_NOT_FOUND", "工具不存在", 404)
    mode = getattr(s, spec.mode_ref)
    if mode == "mock":
        return {"status": "mock", "message": "演示适配器，不代表真实服务连接成功"}
    base = getattr(s, spec.endpoint_ref)
    if not base:
        raise DomainError("TOOL_NOT_CONFIGURED", "工具地址尚未配置", 503)
    try:
        response = await request.app.state.client.get(
            base.rstrip("/") + "/health",
            timeout=3,
            headers={"Authorization": "Bearer " + getattr(s, spec.secret_ref).get_secret_value()},
        )
        response.raise_for_status()
        return {"status": "reachable", "message": "健康端点可达；不代表业务检索验收通过"}
    except Exception as exc:
        raise DomainError("TOOL_UNAVAILABLE", "健康端点不可达，请检查服务配置", 503, True) from exc


@router.get("/admin/services")
async def services(request: Request, user: User):
    admin(user)
    s = request.app.state.settings
    voice = "mock" if s.voice_provider == "mock" else "unconfigured"
    if s.voice_provider == "nvidia" and s.voicechat_health_url:
        try:
            response = await request.app.state.client.get(
                s.voicechat_health_url,
                timeout=3,
                headers={"Authorization": "Bearer " + s.voicechat_api_key.get_secret_value()},
            )
            voice = (
                "healthy"
                if response.status_code == 200 and response.json().get("status") == "ok"
                else "unavailable"
            )
        except Exception:
            voice = "unavailable"
    return {
        "voice_health": voice,
        "text_model": "mock"
        if s.agent_provider == "mock"
        else ("configured" if s.agent_model and s.openai_api_key.get_secret_value() else "unconfigured"),
        "text_note": "配置状态不代表真实推理验收",
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
        "message": "本副本已停止接收新会话；活跃会话在现有时限内结束，随后可停止进程。",
    }
