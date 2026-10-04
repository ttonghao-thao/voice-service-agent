import asyncio
import logging
import time
import weakref

from app.agent_runtime.context import RunContext
from app.agent_runtime.direct import EvidenceReady
from app.agent_runtime.evidence import EvidenceGate
from app.contracts import AnswerBundle, DomainError
from app.storage.models import Conversation, DeliveryAttempt, Turn
from sqlalchemy import select

logger = logging.getLogger(__name__)


class SessionCoordinator:
    def __init__(self, store, runtime, coordination, settings):
        self.store, self.runtime, self.coordination, self.settings = store, runtime, coordination, settings
        self.locks = weakref.WeakValueDictionary()
        self.tasks = {}
        self.all_tasks = set()
        self.awaiting_answers = {}
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
                attempts = (await db.execute(select(DeliveryAttempt).where(
                    DeliveryAttempt.conversation_id == c.id,
                    DeliveryAttempt.status.in_(("prepared", "write_started")),
                ))).scalars()
                for attempt in attempts:
                    attempt.status = "unknown" if attempt.status == "write_started" else "discarded"
                t = await db.get(Turn, c.current_turn) if c.current_turn else None
                if c.voice_session_id or (t and t.status == "running"):
                    if t and t.status == "running":
                        t.status = "expired"
                        t.cancellation_reason = "service_restarted"
                        t.delivery_status = "discarded"
                        t.output_suppressed = True
                        c.request_revision += 1
                    c.epoch += 1
                    c.current_turn, c.voice_session_id = None, None
                    await self.store.event(
                        db, c, "portal.session.ended", {"message": "Service restarted. Restart voice."}
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
                attempts = (await db.execute(select(DeliveryAttempt).where(
                    DeliveryAttempt.conversation_id == cid,
                    DeliveryAttempt.status.in_(("prepared", "write_started")),
                ))).scalars()
                for attempt in attempts:
                    attempt.status = "unknown" if attempt.status == "write_started" else "discarded"
                if c.current_turn:
                    t = await db.get(Turn, c.current_turn)
                    if t and t.status == "running":
                        t.status = "expired"
                        t.cancellation_reason = "gateway_lease_replaced"
                        t.delivery_status = "discarded"
                        t.output_suppressed = True
                        c.request_revision += 1
                c.epoch += 1
                c.current_turn, c.voice_session_id = None, None
                await self.store.event(
                    db, c, "portal.playback.clear", {"message": "A new gateway took over. Restart voice."}
                )

    async def submit(
        self,
        principal,
        cid,
        key,
        request,
        channel="text",
        expected_epoch=None,
        native_call_id=None,
        input_item_id=None,
        decision=None,
    ):
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            if self.draining:
                raise DomainError("SERVICE_DRAINING", "Service is under maintenance. Please try again later.", 503, True)
            if len(self.all_tasks) >= self.settings.max_agent_runs:
                raise DomainError("AGENT_CAPACITY_EXCEEDED", "Search service is busy. Please try again later.", 429, True)
            turn, conversation, created = await self.store.begin_turn(
                principal,
                cid,
                key,
                request,
                channel,
                expected_epoch,
                native_call_id,
                input_item_id,
                decision.tool.name if decision else None,
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
                request_revision=turn.request_revision,
                locale=conversation.locale,
                slots=dict(conversation.slots),
                deadline=time.monotonic() + self.settings.agent_deadline_ms / 1000,
                retrieval_limit=self.settings.qa_max_retrieval_calls if decision else None,
                answer_policy=conversation.answer_policy,
            )
            task = asyncio.create_task(
                self.execute(ctx, request, conversation.history, channel, decision)
            )
            self.tasks[cid] = task
            self.all_tasks.add(task)
            task.add_done_callback(self.all_tasks.discard)
            task.add_done_callback(
                lambda done: self.tasks.pop(cid, None) if self.tasks.get(cid) is done else None
            )
            return turn, task

    async def execute(self, ctx, request, history, channel, decision=None):
        started = time.monotonic()

        async def progress(message):
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                if await self.store.current(
                    ctx.conversation_id,
                    ctx.epoch,
                    ctx.turn_id,
                    ctx.request_revision,
                ):
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
                    if decision:
                        bundle = await self.runtime.run_selected(
                            decision, request, ctx, runtime_history, progress if channel == "text" else None)
                    else:
                        bundle = await self.runtime.run(
                            request, ctx, runtime_history, progress if channel == "text" else None)
            except TimeoutError:
                bundle = self.runtime.failure("AGENT_TIMEOUT", "The request timed out. Please try again later.")
            if isinstance(bundle, EvidenceReady):
                async with self.lock(ctx.conversation_id):
                    await self.coordination.check(ctx.conversation_id)
                    if not await self.store.execution(ctx, bundle):
                        return None
                    future = asyncio.get_running_loop().create_future()
                    self.awaiting_answers[ctx.turn_id] = future
                    continuation = asyncio.create_task(
                        self.finish_provider(ctx, request, visible_history, bundle, future))
                    self.tasks[ctx.conversation_id] = continuation
                    self.all_tasks.add(continuation)
                    continuation.add_done_callback(self.all_tasks.discard)
                    continuation.add_done_callback(lambda done: self.tasks.pop(ctx.conversation_id, None)
                        if self.tasks.get(ctx.conversation_id) is done else None)
                return bundle
            if decision:
                await self.store.execution(ctx)
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
            commit_started = time.monotonic()
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                committed = await self.store.commit(
                    ctx.conversation_id,
                    ctx.epoch,
                    ctx.request_revision,
                    ctx.turn_id,
                    bundle,
                    new_history,
                    ctx.slots,
                )
            logger.info(
                "answer_delivery_finished conversation_id=%s turn_id=%s channel=%s committed=%s commit_ms=%s total_ms=%s",
                ctx.conversation_id, ctx.turn_id, channel, committed,
                round((time.monotonic() - commit_started) * 1000),
                round((time.monotonic() - started) * 1000),
            )
            return bundle if committed else None
        except asyncio.CancelledError:
            raise
        except DomainError as exc:
            if exc.code in ("GATEWAY_LEASE_LOST", "SESSION_OWNED_BY_OTHER"):
                return None
            raise
        except Exception:
            bundle = self.runtime.failure("AGENT_FAILED", "The request failed. Please try again later.")
            if self.coordination.valid:
                async with self.lock(ctx.conversation_id):
                    await self.coordination.check(ctx.conversation_id)
                    await self.store.commit(
                        ctx.conversation_id,
                        ctx.epoch,
                        ctx.request_revision,
                        ctx.turn_id,
                        bundle,
                        history,
                    )
            return bundle if self.coordination.valid else None

    def provider_answer(self, turn_id, text):
        future = self.awaiting_answers.get(turn_id)
        if future and not future.done():
            future.set_result(text)

    async def finish_provider(self, ctx, request, history, ready, future):
        try:
            budget = min(self.settings.qa_provider_answer_timeout_ms / 1000,
                         max(0, ready.deadline - time.monotonic()))
            try:
                text = await asyncio.wait_for(future, budget)
                code = EvidenceGate.check_spoken(text, ready.citations)
            except TimeoutError:
                text, code = "", "VOICE_ANSWER_TIMEOUT"
            allowed = await self.runtime.registry.allowed(ctx.principal)
            if not ctx.invoked <= allowed or any(
                [await self.store.tool_revision(name) != ctx.tool_versions.get(name, 0) for name in ctx.invoked]
            ):
                code = "FORBIDDEN"
            bundle = AnswerBundle(
                answer_id=ready.answer_id, status="failed" if code else "answered",
                display_text="The spoken response could not be verified. Please try again." if code else text,
                speech_text="" if code else text if len(text) <= 160 else "Please read the written answer in the portal.",
                citations=[] if code else ready.citations, reason_code=code,
                answer_kind="knowledge", composition="nano_grounded",
                validation_level="source_checked", verification_timing="after_audio",
                is_mock=any(c.is_mock for c in ready.citations),
            )
            new_history = history + [{"role": "user", "content": request},
                {"role": "assistant", "content": bundle.display_text,
                 "authorized_kb_ids": sorted(ctx.principal.knowledge_base_ids)}]
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                committed = await self.store.commit(ctx.conversation_id, ctx.epoch, ctx.request_revision,
                    ctx.turn_id, bundle, new_history, ctx.slots)
            if committed and code and self.voice:
                response_id = await self.voice.suppress_turn(ctx.conversation_id, ctx.epoch, ctx.turn_id)
                async with self.lock(ctx.conversation_id):
                    await self.coordination.check(ctx.conversation_id)
                    async with self.store.transaction() as db:
                        c = await self.store.get(db, ctx.conversation_id, lock=True)
                        if c.epoch == ctx.epoch and c.request_revision == ctx.request_revision and c.current_turn == ctx.turn_id:
                            await self.store.event(db, c, "portal.playback.clear",
                                {"message": "The spoken response could not be verified.", "response_id": response_id}, ctx.turn_id)
        except asyncio.CancelledError:
            raise
        except DomainError as exc:
            if exc.code not in ("GATEWAY_LEASE_LOST", "SESSION_OWNED_BY_OTHER"):
                logger.error("provider_answer_failed turn_id=%s code=%s", ctx.turn_id, exc.code)
        except Exception as exc:
            logger.error("provider_answer_failed turn_id=%s exception_type=%s", ctx.turn_id, type(exc).__name__)
            # Close the native connection so recovery/invalidation cannot leave a running turn behind.
            if self.voice:
                await self.voice.close_conversation(ctx.conversation_id)
        finally:
            self.awaiting_answers.pop(ctx.turn_id, None)

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

    async def cancel_task(self, principal, cid, expected_epoch, expected_revision):
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            revision, changed = await self.store.cancel_task(
                principal, cid, expected_epoch, expected_revision
            )
            if changed:
                task = self.tasks.pop(cid, None)
                if task:
                    task.cancel()
                # A native call cannot be left pending. Until an installed VoiceChat
                # version proves a safe stale-call result, close that connection.
                if self.voice:
                    await self.voice.close_conversation(cid)
            return revision, changed

    async def stop_playback(
        self, principal, cid, expected_epoch, expected_revision, response_id=None
    ):
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            revision = await self.store.stop_playback(
                principal, cid, expected_epoch, expected_revision, response_id
            )
            if self.voice:
                await self.voice.suppress_playback(cid, expected_epoch, response_id)
            return revision

    async def close(self):
        self.draining = True
        if self.voice:
            await self.voice.close()
        tasks = list(self.all_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
