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
        if c is None or (principal and c.owner_id != principal.user_id):
            raise DomainError("FORBIDDEN", "Conversation not found or access denied", 404)
        return c

    async def owner_for_token(self, cid, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        async with self.sessions() as db:
            c = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == cid,
                        Conversation.access_token_hash == digest,
                    )
                )
            ).scalar_one_or_none()
        if c is None:
            raise DomainError("AUTH_REQUIRED", "Call access is invalid or expired", 401)
        return c.owner_id

    async def revoke_access(self, principal, cid):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            c.access_token_hash = None

    async def event(self, db, c, kind, payload, turn_id=None):
        c.event_seq += 1
        c.updated_at = now()
        e = PortalEvent(
            type=kind,
            conversation_id=c.id,
            epoch=c.epoch,
            request_revision=c.request_revision,
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

    async def begin_turn(
        self,
        principal,
        cid,
        key,
        request,
        channel,
        expected_epoch=None,
        native_call_id=None,
    ):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            existing = (
                await db.execute(select(Turn).where(Turn.conversation_id == cid, Turn.idempotency_key == key))
            ).scalar_one_or_none()
            digest = hashlib.sha256(request.encode()).hexdigest()
            if existing:
                if existing.request_hash != digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "The same request ID cannot be used for different content", 409)
                return existing, c, False
            if expected_epoch is not None and c.epoch != expected_epoch:
                raise DomainError("STALE_EPOCH", "This voice connection has expired", 409)
            parent_task_id = c.current_turn
            if parent_task_id:
                previous = await db.get(Turn, parent_task_id)
                if previous and previous.status == "running":
                    previous.status = "superseded"
                    previous.cancellation_reason = "request_revised"
                    previous.delivery_status = "discarded"
                    previous.output_suppressed = True
            if channel == "text":
                c.epoch += 1
                c.voice_session_id = None
                await self.event(db, c, "portal.playback.clear", {})
            c.request_revision += 1
            t = Turn(
                id=uid(),
                conversation_id=cid,
                epoch=c.epoch,
                request_revision=c.request_revision,
                parent_task_id=parent_task_id,
                native_call_id=native_call_id,
                idempotency_key=key,
                request_hash=digest,
                user_text=request,
                channel=channel,
                status="running",
                delivery_status="pending_validation",
            )
            db.add(t)
            c.current_turn = t.id
            if c.title in ("New conversation", "新会话"):
                c.title = request[:36]
            await self.event(
                db, c, "portal.tool.started", {"message": "Processing", "user_text": request}, t.id
            )
            return t, c, True

    async def commit(self, cid, epoch, request_revision, turn_id, bundle, history, slots=None):
        async with self.transaction() as db:
            c = await self.get(db, cid, lock=True)
            t = await db.get(Turn, turn_id)
            if (
                c.epoch != epoch
                or c.request_revision != request_revision
                or c.current_turn != turn_id
                or t.status != "running"
                or t.request_revision != request_revision
            ):
                return False
            t.status, t.answer = bundle.status, bundle.model_dump(mode="json")
            t.delivery_status = "accepted"
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
                raise DomainError("STALE_EPOCH", "Conversation version mismatch", 409)
            if c.epoch != expected_epoch:
                return c.epoch, False
            if c.current_turn:
                t = await db.get(Turn, c.current_turn)
                if t and t.status == "running":
                    t.status = "canceled"
                    t.cancellation_reason = "hard_interrupt"
                    t.delivery_status = "discarded"
                    t.output_suppressed = True
                    c.request_revision += 1
            c.epoch += 1
            c.current_turn, c.voice_session_id = None, None
            c.summary = ("The previous answer was interrupted; the user may not have heard it all.\n" + c.summary)[:1500]
            await self.event(db, c, "portal.playback.clear", {})
            return c.epoch, True

    async def current(self, cid, epoch, tid=None, request_revision=None):
        async with self.sessions() as db:
            c = await self.get(db, cid)
            return (
                c.epoch == epoch
                and (request_revision is None or c.request_revision == request_revision)
                and (tid is None or c.current_turn == tid)
            )

    async def rotate_voice(self, principal, cid):
        """Fence an old audio connection without inventing a new business revision."""
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            if c.current_turn:
                t = await db.get(Turn, c.current_turn)
                if t and t.status == "running":
                    t.status = "canceled"
                    t.cancellation_reason = "voice_restarted"
                    t.delivery_status = "discarded"
                    t.output_suppressed = True
                    c.request_revision += 1
                    c.current_turn = None
            c.epoch += 1
            c.voice_session_id = None
            await self.event(db, c, "portal.playback.clear", {"message": "Voice connection updated"})
            return c.epoch, c.request_revision

    async def cancel_task(self, principal, cid, expected_epoch, expected_revision, reason="user_cancelled"):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            if c.epoch != expected_epoch:
                raise DomainError("STALE_EPOCH", "Voice connection version mismatch", 409)
            if c.request_revision != expected_revision:
                return c.request_revision, False
            if not c.current_turn:
                return c.request_revision, False
            t = await db.get(Turn, c.current_turn)
            if not t or t.status != "running":
                return c.request_revision, False
            t.status = "canceled"
            t.cancellation_reason = reason
            t.delivery_status = "discarded"
            t.output_suppressed = True
            c.current_turn = None
            c.request_revision += 1
            await self.event(db, c, "portal.playback.clear", {"message": "Current search canceled"})
            return c.request_revision, True

    async def stop_playback(self, principal, cid, expected_epoch, expected_revision, response_id=None):
        async with self.transaction() as db:
            c = await self.get(db, cid, principal, lock=True)
            if c.epoch != expected_epoch:
                raise DomainError("STALE_EPOCH", "Voice connection version mismatch", 409)
            if c.request_revision != expected_revision:
                raise DomainError("STALE_REVISION", "Search revision mismatch", 409)
            if c.current_turn:
                t = await db.get(Turn, c.current_turn)
                if t:
                    t.output_suppressed = True
            await self.event(
                db,
                c,
                "portal.playback.clear",
                {"message": "Playback stopped", "response_id": response_id},
            )
            return c.request_revision

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
