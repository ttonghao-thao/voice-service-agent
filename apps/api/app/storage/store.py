import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta

from app.contracts import DomainError, PortalEvent, now, portal_server_event_adapter, uid
from app.storage.models import Base, Conversation, Event, Record, ToolConfig, Turn
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class Store:
    def __init__(self, url):
        self.write_lock = asyncio.Lock()
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init_dev(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def healthy(self):
        async with self.sessions() as db:
            await db.execute(text("SELECT 1"))
            await db.execute(select(Conversation.id).limit(1))

    @asynccontextmanager
    async def transaction(self):
        async with self.write_lock:
            async with self.sessions.begin() as db:
                yield db

    async def get(self, db, cid, principal=None, lock=False):
        q = select(Conversation).where(Conversation.id == cid)
        if lock:
            q = q.with_for_update()
        c = (await db.execute(q)).scalar_one_or_none()
        if c is None or (
            principal and (c.tenant_id != principal.tenant_id or c.user_id != principal.user_id)
        ):
            raise DomainError("FORBIDDEN", "会话不存在或无权访问", 404)
        return c

    async def event(self, db, c, kind, payload, turn_id=None):
        c.event_seq += 1
        c.updated_at = now()
        e = PortalEvent(
            type=kind,
            conversation_id=c.id,
            epoch=c.epoch,
            turn_id=turn_id,
            server_seq=c.event_seq,
            payload=payload,
        )
        portal_server_event_adapter.validate_python(e.model_dump(mode="json"))
        db.add(
            Event(
                id=e.event_id,
                conversation_id=c.id,
                server_seq=e.server_seq,
                payload=e.model_dump(mode="json"),
            )
        )
        return e

    async def begin_turn(self, principal, cid, key, request, channel, expected_epoch=None):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            existing = (
                await db.execute(select(Turn).where(Turn.conversation_id == cid, Turn.idempotency_key == key))
            ).scalar_one_or_none()
            digest = hashlib.sha256(request.encode()).hexdigest()
            if existing:
                if existing.request_hash != digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "同一个请求标识不能用于不同内容", 409)
                return existing, c, False
            if expected_epoch is not None and c.epoch != expected_epoch:
                raise DomainError("STALE_EPOCH", "该语音连接已失效", 409)
            if c.current_turn:
                previous = await db.get(Turn, c.current_turn)
                if previous and previous.status == "running":
                    previous.status = "canceled"
            if channel == "text":
                c.epoch += 1
                c.voice_session_id = None
                await self.event(db, c, "portal.playback.clear", {})
            t = Turn(
                id=uid(),
                conversation_id=cid,
                epoch=c.epoch,
                idempotency_key=key,
                request_hash=digest,
                user_text=request,
                channel=channel,
                status="running",
            )
            db.add(t)
            c.current_turn = t.id
            if c.title == "新会话":
                c.title = request[:36]
            await self.event(
                db, c, "portal.tool.started", {"message": "正在处理", "user_text": request}, t.id
            )
            return t, c, True

    async def commit(self, cid, epoch, turn_id, bundle, history, slots=None):
        async with self.transaction() as db:
            c = await self.get(db, cid, lock=True)
            t = await db.get(Turn, turn_id)
            if c.epoch != epoch or c.current_turn != turn_id or t.status != "running":
                return False
            t.status, t.answer = bundle.status, bundle.model_dump(mode="json")
            # Commit only validated user/assistant history, never partial SDK tool messages.
            if bundle.status in ("answered", "needs_clarification", "insufficient_evidence"):
                c.history = history[-24:]
                if slots is not None:
                    c.slots = slots
                c.summary = "\n".join(str(x.get("content", "")) for x in c.history[-6:])[-1500:]
            await self.event(db, c, "portal.answer.final", t.answer, turn_id)
            return True

    async def invalidate(self, principal, cid, expected_epoch):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            if c.epoch < expected_epoch:
                raise DomainError("STALE_EPOCH", "会话版本不匹配", 409)
            if c.epoch != expected_epoch:
                return c.epoch, False
            if c.current_turn:
                t = await db.get(Turn, c.current_turn)
                if t and t.status == "running":
                    t.status = "canceled"
            c.epoch += 1
            c.current_turn, c.voice_session_id = None, None
            c.summary = ("上个回答被打断；不代表用户已听到完整内容。\n" + c.summary)[:1500]
            await self.event(db, c, "portal.playback.clear", {})
            return c.epoch, True

    async def current(self, cid, epoch, tid=None):
        async with self.sessions() as db:
            c = await self.get(db, cid)
            return c.epoch == epoch and (tid is None or c.current_turn == tid)

    async def record(self, cid, epoch, kind, source_id, payload):
        async with self.transaction() as db:
            c = await self.get(db, cid, lock=True)
            if c.epoch != epoch:
                return False
            old = (
                await db.execute(
                    select(Record).where(
                        Record.conversation_id == cid,
                        Record.epoch == epoch,
                        Record.kind == kind,
                        Record.source_id == source_id,
                    )
                )
            ).scalar_one_or_none()
            if old:
                if kind == "playback_ack":
                    if payload["played_samples"] >= old.payload.get("played_samples", 0):
                        old.payload = payload
                return False
            db.add(Record(conversation_id=cid, epoch=epoch, kind=kind, source_id=source_id, payload=payload))
            return True

    async def enabled(self, name):
        async with self.sessions() as db:
            c = await db.get(ToolConfig, name)
            return c is None or c.enabled

    async def tool_revision(self, name):
        async with self.sessions() as db:
            c = await db.get(ToolConfig, name)
            return c.revision if c else 0

    async def purge(self, days):
        async with self.transaction() as db:
            cutoff = now() - timedelta(days=days)
            from app.storage.models import AdminAudit

            await db.execute(delete(AdminAudit).where(AdminAudit.created_at < cutoff))
            # Explicit child deletion also supports SQLite development without FK pragmas.
            ids = select(Conversation.id).where(
                Conversation.updated_at < cutoff, Conversation.voice_session_id.is_(None)
            )
            from app.storage.models import ToolRun

            for model in (Event, Record, ToolRun, Turn):
                await db.execute(delete(model).where(model.conversation_id.in_(ids)))
            await db.execute(delete(Conversation).where(Conversation.id.in_(ids)))
