import asyncio
import weakref

from app.agent_runtime.context import RunContext
from app.contracts import DomainError
from app.storage.models import Conversation, Turn
from sqlalchemy import select


class SessionCoordinator:
    def __init__(self, store, runtime, coordination, settings):
        self.store, self.runtime, self.coordination, self.settings = store, runtime, coordination, settings
        self.locks = weakref.WeakValueDictionary()
        self.tasks = {}
        self.all_tasks = set()
        self.voice = None
        self.draining = False

    def lock(self, cid):
        lock = self.locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[cid] = lock
        return lock

    def authorized_history(self, history, principal):
        current = set(principal.knowledge_base_ids)
        configured = {
            value.strip() for value in self.settings.knowledge_base_ids.split(",") if value.strip()
        }
        visible = []
        for item in history:
            scope = item.get("authorized_kb_ids")
            if item.get("role") == "assistant":
                if scope is None and ("customer" in principal.roles or current != configured):
                    continue
                if scope is not None and not set(scope).issubset(current):
                    continue
            visible.append(dict(item))
        return visible

    async def recover(self):
        # Distributed deployments recover on first ownership acquisition, never invalidate
        # conversations still owned by another healthy gateway.
        if self.coordination.redis:
            return
        async with self.store.transaction() as db:
            rows = (await db.execute(select(Conversation))).scalars()
            for c in rows:
                t = await db.get(Turn, c.current_turn) if c.current_turn else None
                if c.voice_session_id or (t and t.status == "running"):
                    if t and t.status == "running":
                        t.status = "canceled"
                    c.epoch += 1
                    c.current_turn, c.voice_session_id = None, None
                    await self.store.event(
                        db, c, "portal.session.ended", {"message": "服务已重启，请重新开始语音"}
                    )

    async def ensure_owner(self, principal, cid):
        # Called under the process-local conversation lock. Authorization precedes claiming.
        async with self.store.sessions() as db:
            await self.store.get(db, cid, principal)
        fresh = await self.coordination.acquire(cid)
        if fresh and self.coordination.redis:
            async with self.store.transaction() as db:
                c = await self.store.get(db, cid, principal, lock=True)
                await self.coordination.check(cid)
                if c.current_turn:
                    t = await db.get(Turn, c.current_turn)
                    if t and t.status == "running":
                        t.status = "canceled"
                c.epoch += 1
                c.current_turn, c.voice_session_id = None, None
                await self.store.event(
                    db, c, "portal.playback.clear", {"message": "新网关已接管，请重新开始语音"}
                )

    async def submit(self, principal, cid, key, request, channel="text", expected_epoch=None):
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            if self.draining:
                raise DomainError("SERVICE_DRAINING", "服务正在维护，请稍后重试", 503, True)
            if len(self.all_tasks) >= self.settings.max_agent_runs:
                raise DomainError("AGENT_CAPACITY_EXCEEDED", "查询服务繁忙，请稍后重试", 429, True)
            turn, conversation, created = await self.store.begin_turn(
                principal, cid, key, request, channel, expected_epoch
            )
            if not created:
                return turn, None
            old = self.tasks.pop(cid, None)
            if old:
                old.cancel()
            if channel == "text" and self.voice:
                await self.voice.close_conversation(cid)
            ctx = RunContext(
                principal,
                cid,
                turn.id,
                turn.epoch,
                locale=conversation.locale,
                slots=dict(conversation.slots),
            )
            task = asyncio.create_task(
                self.execute(ctx, request, conversation.history, channel)
            )
            self.tasks[cid] = task
            self.all_tasks.add(task)
            task.add_done_callback(self.all_tasks.discard)
            task.add_done_callback(
                lambda done: self.tasks.pop(cid, None) if self.tasks.get(cid) is done else None
            )
            return turn, task

    async def execute(self, ctx, request, history, channel):
        async def progress(message):
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                if await self.store.current(ctx.conversation_id, ctx.epoch, ctx.turn_id):
                    async with self.store.transaction() as db:
                        c = await self.store.get(db, ctx.conversation_id, lock=True)
                        await self.store.event(
                            db, c, "portal.tool.started", {"message": message}, ctx.turn_id
                        )

        try:
            visible_history = self.authorized_history(history, ctx.principal)
            runtime_history = [
                {"role": item["role"], "content": item["content"]}
                for item in visible_history
            ]
            try:
                async with asyncio.timeout(self.settings.agent_deadline_ms / 1000):
                    bundle = await self.runtime.run(
                        request, ctx, runtime_history, progress if channel == "text" else None
                    )
            except TimeoutError:
                bundle = self.runtime.failure("AGENT_TIMEOUT", "处理超时，请稍后重试。")
            answer_scope = sorted(
                {kb for citation in bundle.citations for kb in citation.authorized_kb_ids}
            )
            new_history = visible_history + [
                {"role": "user", "content": request},
                {
                    "role": "assistant",
                    "content": bundle.display_text,
                    "authorized_kb_ids": answer_scope,
                },
            ]
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                committed = await self.store.commit(
                    ctx.conversation_id, ctx.epoch, ctx.turn_id, bundle, new_history, ctx.slots
                )
            return bundle if committed else None
        except asyncio.CancelledError:
            raise
        except DomainError as exc:
            if exc.code in ("GATEWAY_LEASE_LOST", "SESSION_OWNED_BY_OTHER"):
                return None
            raise
        except Exception:
            bundle = self.runtime.failure("AGENT_FAILED", "业务处理失败，请稍后重试。")
            if self.coordination.valid:
                async with self.lock(ctx.conversation_id):
                    await self.coordination.check(ctx.conversation_id)
                    await self.store.commit(ctx.conversation_id, ctx.epoch, ctx.turn_id, bundle, history)
            return bundle if self.coordination.valid else None

    async def interrupt(self, principal, cid, expected_epoch):
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            epoch, changed = await self.store.invalidate(principal, cid, expected_epoch)
            if changed:
                task = self.tasks.pop(cid, None)
                if task:
                    task.cancel()
                if self.voice:
                    await self.voice.close_conversation(cid)
            return epoch

    async def close(self):
        self.draining = True
        if self.voice:
            await self.voice.close()
        tasks = list(self.all_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
