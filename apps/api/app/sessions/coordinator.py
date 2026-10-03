import asyncio
import logging
import time
import weakref
from dataclasses import dataclass

from app.agent_runtime.context import BusinessInput, RunContext
from app.contracts import ACTIVE_TURN_STATUSES, AnswerBundle, DomainError, KnowledgeBundle
from app.storage.models import Conversation, Turn
from sqlalchemy import select

logger = logging.getLogger(__name__)


@dataclass
class TurnExecution:
    ctx: RunContext
    ready: asyncio.Future
    voice_completion: asyncio.Future
    task: asyncio.Task | None = None
    response_id: str | None = None


class SessionCoordinator:
    def __init__(self, store, runtime, coordination, settings, profile=None, prepare_reply=None):
        self.store, self.runtime, self.coordination, self.settings = store, runtime, coordination, settings
        self.profile = profile or settings.execution_profile()
        from app.voice.provider import VoiceProfile

        self.prepare_reply = prepare_reply or VoiceProfile.build(self.profile).prepare_reply
        self.accept_text = self._accept_text if self.profile.text_available else self._reject_text
        self.executions = {}
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
        configured = {value.strip() for value in self.settings.knowledge_base_ids.split(",") if value.strip()}
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
                if c.voice_session_id or (t and t.status in ACTIVE_TURN_STATUSES):
                    if t and t.status in ACTIVE_TURN_STATUSES:
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
                if c.current_turn:
                    t = await db.get(Turn, c.current_turn)
                    if t and t.status in ACTIVE_TURN_STATUSES:
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

    async def _accept_text(self, principal, cid):
        pass

    async def _reject_text(self, principal, cid):
        async with self.store.sessions() as db:
            await self.store.get(db, cid, principal)
        raise DomainError(
            "TEXT_INPUT_UNAVAILABLE", "Text input is unavailable in this deployment. Please use voice.", 409
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
    ):
        if channel == "text":
            await self.accept_text(principal, cid)
        business_input = request if isinstance(request, BusinessInput) else BusinessInput(request)
        request = business_input.user_text
        async with self.lock(cid):
            await self.ensure_owner(principal, cid)
            if self.draining:
                raise DomainError(
                    "SERVICE_DRAINING", "Service is under maintenance. Please try again later.", 503, True
                )
            if len(self.all_tasks) >= self.settings.max_agent_runs:
                raise DomainError(
                    "AGENT_CAPACITY_EXCEEDED", "Search service is busy. Please try again later.", 429, True
                )
            turn, conversation, created = await self.store.begin_turn(
                principal,
                cid,
                key,
                request,
                channel,
                expected_epoch,
                native_call_id,
                input_item_id,
                self.profile.mode,
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
            )
            loop = asyncio.get_running_loop()
            execution = TurnExecution(ctx, loop.create_future(), loop.create_future())
            self.executions[turn.id] = execution
            task = asyncio.create_task(self.execute(execution, business_input, conversation.history, channel))
            execution.task = task
            self.tasks[cid] = task
            self.all_tasks.add(task)
            task.add_done_callback(self.all_tasks.discard)
            task.add_done_callback(
                lambda done: self.tasks.pop(cid, None) if self.tasks.get(cid) is done else None
            )
            return turn, execution.ready

    async def check_tools(self, ctx):
        allowed = await self.runtime.registry.allowed(ctx.principal)
        checked = ctx.invoked or (
            {"search_knowledge"}
            if self.profile.mode == "direct" and "search_knowledge" in ctx.allowed_tools
            else set()
        )
        for name in checked:
            if name not in allowed or await self.store.tool_revision(name) != ctx.tool_versions[name]:
                raise DomainError("FORBIDDEN", "Knowledge access changed. Please ask again.", 403)

    def active_voice(self, cid):
        return self.profile.mode == "direct" and any(
            e.ctx.conversation_id == cid and (e.task is None or not e.task.done())
            for e in self.executions.values()
        )

    def generating_voice(self, cid):
        return self.profile.mode == "direct" and any(
            e.ctx.conversation_id == cid and not e.voice_completion.done()
            for e in self.executions.values()
        )

    def bind_voice_response(self, cid, epoch, revision, turn_id, response_id):
        e = self.executions.get(turn_id)
        if not e or (e.ctx.conversation_id, e.ctx.epoch, e.ctx.request_revision) != (cid, epoch, revision):
            return False
        if e.response_id is None:
            e.response_id = response_id
        return e.response_id == response_id and not e.voice_completion.done()

    async def complete_voice(self, cid, epoch, revision, turn_id, response_id):
        e = self.executions.get(turn_id)
        if (
            self.profile.mode != "direct"
            or not e
            or e.response_id != response_id
            or (e.ctx.conversation_id, e.ctx.epoch, e.ctx.request_revision) != (cid, epoch, revision)
            or e.voice_completion.done()
            or not await self.store.current(cid, epoch, turn_id, revision)
        ):
            return False
        async with self.store.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if not turn or turn.status != "awaiting_voice" or turn.delivery_status != "tool_submitted":
                return False
        if e.voice_completion.done():
            return False
        e.voice_completion.set_result((response_id, None))
        return True

    async def fail_voice(self, cid, epoch, revision, turn_id, reason_code):
        e = self.executions.get(turn_id)
        if (
            self.profile.mode != "direct"
            or not e
            or (e.ctx.conversation_id, e.ctx.epoch, e.ctx.request_revision) != (cid, epoch, revision)
            or e.voice_completion.done()
        ):
            return False
        e.voice_completion.set_result((None, reason_code))
        return True

    async def execute(self, execution, request, history, channel):
        ctx = execution.ctx
        started, knowledge = time.monotonic(), None
        visible_history = self.authorized_history(history, ctx.principal)

        async def progress(message):
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                if await self.store.current(
                    ctx.conversation_id, ctx.epoch, ctx.turn_id, ctx.request_revision
                ):
                    async with self.store.transaction() as db:
                        c = await self.store.get(db, ctx.conversation_id, lock=True)
                        await self.store.event(
                            db, c, "portal.tool.started", {"message": message}, ctx.turn_id
                        )

        def history_with(bundle, scope):
            return visible_history + [
                {"role": "user", "content": request.user_text, "turn_id": ctx.turn_id},
                {
                    "role": "assistant",
                    "content": bundle.display_text,
                    "authorized_kb_ids": scope,
                    "turn_id": ctx.turn_id,
                },
            ]

        async def commit_failure(code):
            bundle = self.runtime.failure(
                code,
                "Voice reply is unavailable. Please restart the call or contact a representative."
                if knowledge
                else "The request failed. Please try again later.",
            )
            if knowledge:
                bundle.answer_origin, bundle.evidence_role = "voicechat", "retrieved_context"
                bundle.authorized_kb_ids = knowledge.authorized_kb_ids
                bundle.citations = (
                    knowledge.citations if code not in ("FORBIDDEN", "KB_ACCESS_REVOKED") else []
                )
                if self.voice:
                    await self.voice.close_conversation(ctx.conversation_id)
            async with self.lock(ctx.conversation_id):
                await self.coordination.check(ctx.conversation_id)
                committed = await (
                    self.store.finalize_voice(
                        ctx.conversation_id,
                        ctx.epoch,
                        ctx.request_revision,
                        ctx.turn_id,
                        bundle,
                        visible_history,
                        voice_failed=True,
                    )
                    if knowledge
                    else self.store.commit(
                        ctx.conversation_id,
                        ctx.epoch,
                        ctx.request_revision,
                        ctx.turn_id,
                        bundle,
                        visible_history,
                    )
                )
            if not execution.ready.done():
                execution.ready.set_result(self.prepare_reply(bundle) if committed else None)
            if knowledge:
                logger.info(
                    "voice_answer_failed conversation_id=%s turn_id=%s mode=direct reason_code=%s total_ms=%s",
                    ctx.conversation_id,
                    ctx.turn_id,
                    code,
                    round((time.monotonic() - started) * 1000),
                )
            return bundle if committed else None

        try:
            async with asyncio.timeout(self.settings.agent_deadline_ms / 1000):
                bundle = await self.runtime.run(
                    request,
                    ctx,
                    [{"role": x["role"], "content": x["content"]} for x in visible_history],
                    progress if channel == "text" else None,
                )
                await self.check_tools(ctx)
                prepared = self.prepare_reply(bundle)
                if isinstance(prepared.result, KnowledgeBundle) and isinstance(bundle, KnowledgeBundle):
                    knowledge = prepared.result
                    async with self.lock(ctx.conversation_id):
                        await self.coordination.check(ctx.conversation_id)
                        committed = await self.store.commit_knowledge(
                            ctx.conversation_id, ctx.epoch, ctx.request_revision, ctx.turn_id, knowledge
                        )
                    execution.ready.set_result(prepared if committed else None)
                    if not committed:
                        return None
                    logger.info(
                        "evidence_committed conversation_id=%s turn_id=%s mode=direct",
                        ctx.conversation_id,
                        ctx.turn_id,
                    )
                    response_id, failure = await execution.voice_completion
                    if failure:
                        return await commit_failure(failure)
                    text = await self.store.voice_text(
                        ctx.conversation_id, ctx.epoch, ctx.request_revision, ctx.turn_id, response_id
                    )
                    if not text:
                        return await commit_failure("VOICE_ANSWER_MISSING")
                    if len(text) > 8000:
                        return await commit_failure("VOICE_ANSWER_TOO_LARGE")
                    await self.check_tools(ctx)
                    bundle = AnswerBundle(
                        status={
                            "answer_from_evidence": "voice_completed",
                            "ask_clarification": "needs_clarification",
                            "report_insufficient": "insufficient_evidence",
                            "report_failure": "failed",
                        }[knowledge.directive],
                        display_text=text,
                        speech_text=text,
                        answer_origin="voicechat",
                        evidence_role="retrieved_context",
                        authorized_kb_ids=knowledge.authorized_kb_ids,
                        citations=knowledge.citations,
                        is_mock=knowledge.is_mock,
                        reason_code=knowledge.reason_code,
                    )
                    async with self.lock(ctx.conversation_id):
                        await self.coordination.check(ctx.conversation_id)
                        committed = await self.store.finalize_voice(
                            ctx.conversation_id,
                            ctx.epoch,
                            ctx.request_revision,
                            ctx.turn_id,
                            bundle,
                            history_with(bundle, knowledge.authorized_kb_ids),
                        )
                    logger.info(
                        "voice_answer_completed conversation_id=%s turn_id=%s mode=direct committed=%s total_ms=%s",
                        ctx.conversation_id,
                        ctx.turn_id,
                        committed,
                        round((time.monotonic() - started) * 1000),
                    )
                    return bundle if committed else None
                scope = sorted({kb for c in bundle.citations for kb in c.authorized_kb_ids})
                async with self.lock(ctx.conversation_id):
                    await self.coordination.check(ctx.conversation_id)
                    committed = await self.store.commit(
                        ctx.conversation_id,
                        ctx.epoch,
                        ctx.request_revision,
                        ctx.turn_id,
                        bundle,
                        history_with(bundle, scope),
                        ctx.slots,
                    )
                execution.ready.set_result(prepared if committed else None)
                logger.info(
                    "answer_delivery_finished conversation_id=%s turn_id=%s mode=%s channel=%s committed=%s total_ms=%s",
                    ctx.conversation_id,
                    ctx.turn_id,
                    self.profile.mode,
                    channel,
                    committed,
                    round((time.monotonic() - started) * 1000),
                )
                return bundle if committed else None
        except TimeoutError:
            return await commit_failure("VOICE_ANSWER_TIMEOUT" if knowledge else "AGENT_TIMEOUT")
        except asyncio.CancelledError:
            raise
        except DomainError as exc:
            if exc.code in ("GATEWAY_LEASE_LOST", "SESSION_OWNED_BY_OTHER"):
                return None
            return await commit_failure(exc.code)
        except Exception as exc:
            logger.error(
                "business_execution_failed conversation_id=%s turn_id=%s mode=%s exception_type=%s",
                ctx.conversation_id,
                ctx.turn_id,
                self.profile.mode,
                type(exc).__name__,
            )
            if self.coordination.valid:
                return await commit_failure("AGENT_FAILED")
        finally:
            if not execution.ready.done():
                execution.ready.set_result(None)
            if not execution.voice_completion.done():
                execution.voice_completion.cancel()
            self.executions.pop(ctx.turn_id, None)

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

    async def stop_playback(self, principal, cid, expected_epoch, expected_revision, response_id=None):
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
        direct_tasks = []
        for e in list(self.executions.values()):
            if self.profile.mode == "direct":
                await self.fail_voice(
                    e.ctx.conversation_id,
                    e.ctx.epoch,
                    e.ctx.request_revision,
                    e.ctx.turn_id,
                    "VOICE_SERVICE_SHUTDOWN",
                )
                direct_tasks.append(e.task)
        if self.voice:
            await self.voice.close()
        if direct_tasks:
            try:
                async with asyncio.timeout(3):
                    await asyncio.gather(*(asyncio.shield(t) for t in direct_tasks), return_exceptions=True)
            except TimeoutError:
                pass
        tasks = list(self.all_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
