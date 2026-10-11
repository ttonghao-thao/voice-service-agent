import asyncio
import hashlib
import json
import logging
import re
import time

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    RunHooks,
    Runner,
)
from app.config import ROOT
from app.contracts import AgentAnswer, AnswerBundle, now, printable_ascii
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class TimingHooks(RunHooks):
    """Per-run timings only; never log prompts, tool arguments or model output."""

    def __init__(self):
        self.calls = 0
        self.started = None

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        self.calls += 1
        self.started = time.monotonic()

    async def on_llm_end(self, context, agent, response):
        self.finish(context.context, "completed")

    def finish(self, ctx, status):
        if self.started is None:
            return
        logger.info(
            "agent_model_call_finished conversation_id=%s turn_id=%s call_index=%s status=%s duration_ms=%s",
            ctx.conversation_id, ctx.turn_id, self.calls, status,
            round((time.monotonic() - self.started) * 1000),
        )
        self.started = None


class BusinessRuntime:
    def __init__(self, settings, registry):
        self.settings, self.registry = settings, registry
        self.prompt = (ROOT / "config/agent-prompt.txt").read_text()
        self.client = None
        if settings.openai_api_key.get_secret_value():
            self.client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.agent_base_url,
                max_retries=0,
                timeout=settings.agent_deadline_ms / 1000,
            )

    async def run(self, request, ctx, history, progress=None):
        ctx.allowed_tools = await self.registry.allowed(ctx.principal)
        ctx.tool_versions = {
            name: await self.registry.store.tool_revision(name) for name in ctx.allowed_tools
        }
        logger.info(
            "agent_run_started conversation_id=%s turn_id=%s provider=%s allowed_tools=%s",
            ctx.conversation_id,
            ctx.turn_id,
            self.settings.agent_provider,
            ",".join(sorted(ctx.allowed_tools)),
        )
        if "search_knowledge" not in ctx.allowed_tools:
            return self.failure(
                "AGENT_NO_AUTHORIZED_TOOL",
                "No authorized knowledge search is available for this request.",
            )
        if self.settings.agent_provider == "mock":
            result = await self.registry.invoke("search_knowledge", {"query": request}, ctx)
            citations = list(ctx.evidence.values())
            return AnswerBundle(
                status="answered" if citations else "insufficient_evidence",
                display_text=(
                    "[Demo mode: text model was not called] "
                    + (
                        citations[0].content + " [C1]"
                        if citations
                        else 'Insufficient evidence. Enter "integration sample" to inspect a synthetic citation.'
                    )
                ),
                speech_text="This is a synthetic integration result, not a real business answer.",
                citations=citations,
                is_mock=True,
                reason_code=None if result.get("hits") else "CUEKB_NO_EVIDENCE",
            )
        if not self.client or not self.settings.agent_model:
            return self.failure(
                "AGENT_NOT_CONFIGURED", "The text model is not configured for business answers."
            )
        tools = []
        for name in sorted(ctx.allowed_tools):
            spec = self.registry.specs[name]
            adapter = self.registry.adapters[spec.adapter_id]

            async def invoke(wrapper, args, tool_name=name):
                logger.info(
                    "agent_tool_call_received conversation_id=%s turn_id=%s tool=%s",
                    ctx.conversation_id,
                    ctx.turn_id,
                    tool_name,
                )
                if progress:
                    await progress("Searching " + self.registry.specs[tool_name].display_name)
                return json.dumps(
                    await self.registry.invoke(tool_name, json.loads(args), wrapper.context),
                    ensure_ascii=False,
                )

            schema = adapter.input_model.model_json_schema()
            # SDK strict schema utility handles nullable default fields and required property lists.
            from agents.strict_schema import ensure_strict_json_schema

            tools.append(
                FunctionTool(
                    name=name,
                    description=spec.description,
                    params_json_schema=ensure_strict_json_schema(schema),
                    on_invoke_tool=invoke,
                )
            )
        model_class = (
            OpenAIChatCompletionsModel
            if self.settings.agent_provider == "compatible"
            else OpenAIResponsesModel
        )
        agent = Agent(
            name="Customer support business assistant",
            instructions=self.prompt
            + "\nCurrent server UTC time: "
            + now().isoformat()
            + "\nConfirmed query filters: "
            + json.dumps(ctx.slots, ensure_ascii=False),
            model=model_class(model=self.settings.agent_model, openai_client=self.client),
            tools=tools,
            output_type=AgentAnswer,
            # BusinessRuntime receives only customer-support requests. Require a
            # trusted tool on the first model step; Agent resets the choice after
            # the call so the following step can produce the structured answer.
            model_settings=ModelSettings(tool_choice="required", parallel_tool_calls=False),
            reset_tool_choice=True,
        )
        inputs = history + [{"role": "user", "content": request}]
        stream = None
        timings = TimingHooks()
        started = time.monotonic()
        run_status = "failed"
        try:
            async with asyncio.timeout(self.settings.agent_deadline_ms / 1000):
                if progress:
                    stream = Runner.run_streamed(
                        agent, inputs, context=ctx, max_turns=8, hooks=timings, run_config=RunConfig(tracing_disabled=True)
                    )
                    async for _ in stream.stream_events():
                        pass  # Raw deltas and reasoning are never exposed before evidence validation.
                    raw = stream.final_output
                    completed_run = stream
                else:
                    result = await Runner.run(
                        agent, inputs, context=ctx, max_turns=8, hooks=timings, run_config=RunConfig(tracing_disabled=True)
                    )
                    raw = result.final_output
                    completed_run = result
                answer = AgentAnswer.model_validate(raw)
                validated = await self.validate(answer, ctx)
                if validated.reason_code == "RAG_INVALID_CITATION":
                    # Keep the original deadline, evidence and context. Repair cannot retrieve again.
                    if not await self.registry.store.current(
                        ctx.conversation_id, ctx.epoch, ctx.turn_id, ctx.request_revision
                    ):
                        run_status = "canceled"
                        return self.failure("STALE_EPOCH", "This search was canceled.")
                    if not ctx.invoked <= await self.registry.allowed(ctx.principal):
                        return self.failure("FORBIDDEN", "Knowledge access changed. Please ask again.")
                    repair_agent = agent.clone(
                        tools=[],
                        model_settings=ModelSettings(tool_choice="none", parallel_tool_calls=False),
                        instructions=agent.instructions + "\nCorrect the previous answer's citation structure once. "
                        "Use only evidence already returned in this turn. Do not retrieve or invent sources. "
                        "Every [Cn] in display_text must be declared in citation_ids; use only the allowed "
                        "citation IDs supplied below, never document IDs or history citations. Re-evaluate "
                        "support: if existing evidence cannot support the answer, return insufficient_evidence "
                        "without unsupported factual claims or invalid citations. Never relabel an unsupported "
                        "claim with an unrelated valid citation.",
                    )
                    logger.info(
                        "agent_citation_repair_started conversation_id=%s turn_id=%s attempt=1",
                        ctx.conversation_id, ctx.turn_id,
                    )
                    repaired = await Runner.run(
                        repair_agent,
                        completed_run.to_input_list() + [{
                            "role": "user",
                            "content": "Correct the citation validation failure. Allowed citation IDs: "
                            + json.dumps(sorted(ctx.evidence))
                            + ". Failure types: " + ",".join(self.citation_issues(answer, ctx)),
                        }],
                        context=ctx, max_turns=1, hooks=timings,
                        run_config=RunConfig(tracing_disabled=True),
                    )
                    validated = await self.validate(AgentAnswer.model_validate(repaired.final_output), ctx)
                    logger.info(
                        "agent_citation_repair_finished conversation_id=%s turn_id=%s status=%s reason_code=%s",
                        ctx.conversation_id, ctx.turn_id, validated.status, validated.reason_code or "none",
                    )
            run_status = validated.status
            logger.info(
                "agent_run_finished conversation_id=%s turn_id=%s status=%s invoked_tools=%s reason_code=%s",
                ctx.conversation_id,
                ctx.turn_id,
                validated.status,
                ",".join(sorted(ctx.invoked)),
                validated.reason_code or "none",
            )
            return validated
        except TimeoutError:
            run_status = "timeout"
            logger.warning(
                "agent_run_timed_out conversation_id=%s turn_id=%s reason_code=AGENT_TIMEOUT budget_ms=%s",
                ctx.conversation_id, ctx.turn_id, self.settings.agent_deadline_ms,
            )
            return self.failure("AGENT_TIMEOUT", "The request timed out. Please try again later.")
        except asyncio.CancelledError:
            run_status = "canceled"
            raise
        except Exception as exc:
            # Do not propagate provider exceptions: their payloads can contain private prompts and credentials.
            logger.error(
                "agent_run_failed conversation_id=%s turn_id=%s exception_type=%s",
                ctx.conversation_id,
                ctx.turn_id,
                type(exc).__name__,
            )
            return self.failure("AGENT_FAILED", "The request failed. Please try again later.")
        finally:
            timings.finish(ctx, run_status)
            logger.info(
                "agent_pipeline_finished conversation_id=%s turn_id=%s status=%s model_calls=%s duration_ms=%s",
                ctx.conversation_id, ctx.turn_id, run_status, timings.calls,
                round((time.monotonic() - started) * 1000),
            )
            if stream is not None and not stream.is_complete:
                stream.cancel(mode="immediate")

    @staticmethod
    def citation_issues(answer, ctx):
        references = set(re.findall(r"\[(C\d+)\]", answer.display_text))
        issues = []
        if references - set(answer.citation_ids):
            issues.append("undeclared_reference")
        if set(answer.citation_ids) - ctx.evidence.keys():
            issues.append("unknown_citation")
        return issues

    @staticmethod
    def citation_log_ids(values):
        # Provider strings are untrusted: never emit arbitrary identifiers, text or log newlines.
        values = sorted(set(values))
        return json.dumps({
            "count": len(values),
            "ids": [value if re.fullmatch(r"C[0-9]{1,8}", value) else
                    "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]
                    for value in values[:20]],
            "omitted": max(0, len(values) - 20),
        }, separators=(",", ":"))

    async def validate(self, answer, ctx):
        if "search_knowledge" in ctx.allowed_tools and "search_knowledge" not in ctx.invoked:
            return self.failure(
                "AGENT_REQUIRED_TOOL_NOT_CALLED",
                "Knowledge search was not executed, so I cannot provide a reliable answer right now.",
            )
        if ctx.tool_errors:
            code = ctx.tool_errors[-1]
            configuration_errors = {
                "CUEKB_AUTH_FAILED",
                "CUEKB_FORBIDDEN",
                "CUEKB_CONTRACT_ERROR",
                "TOOL_NOT_CONFIGURED",
            }
            message = (
                "Knowledge access is not configured correctly. Please contact a representative."
                if code in configuration_errors
                else "The knowledge search failed temporarily. Please try again later."
            )
            return self.failure(code, message)
        if not all(
            [
                await self.registry.store.enabled(name)
                and self.registry.deployment_enabled(name)
                and await self.registry.store.tool_revision(name) == ctx.tool_versions.get(name, 0)
                for name in ctx.invoked
            ]
        ):
            return self.failure("FORBIDDEN", "Knowledge access changed. Please ask again.")
        issues = self.citation_issues(answer, ctx)
        if issues:
            logger.warning(
                "agent_citation_validation_failed conversation_id=%s turn_id=%s "
                "reason_code=RAG_INVALID_CITATION failure_types=%s available=%s referenced=%s declared=%s",
                ctx.conversation_id, ctx.turn_id, ",".join(issues),
                self.citation_log_ids(ctx.evidence),
                self.citation_log_ids(re.findall(r"\[(C\d+)\]", answer.display_text)),
                self.citation_log_ids(answer.citation_ids),
            )
            return self.failure(
                "RAG_INVALID_CITATION",
                "Source validation failed, so I cannot provide a reliable answer right now.",
            )
        # `failed` is a service-owned execution state. Some compatible models
        # still emit it despite a successful tool call, which previously left a
        # cited answer carrying a contradictory Search failed status. Accept the
        # provider output for compatibility, then derive the business status from
        # the validated evidence. Actual runtime/tool failures returned above.
        status = answer.status
        reason_code = None
        if status == "failed":
            status = "answered" if answer.citation_ids or ctx.cards else "insufficient_evidence"
            reason_code = None if status == "answered" else "CUEKB_NO_EVIDENCE"
            logger.info(
                "agent_status_normalized conversation_id=%s turn_id=%s model_status=failed status=%s",
                ctx.conversation_id,
                ctx.turn_id,
                status,
            )
        if status == "answered":
            conflicting = any(
                item.get("evidence_status") in ("insufficient", "conflicting")
                or item.get("retrieval_status") in ("not_found", "needs_clarification")
                for item in ctx.retrievals
            )
            if conflicting:
                return AnswerBundle(
                    status="insufficient_evidence",
                    display_text="The available knowledge is insufficient or conflicting. Please clarify or contact a representative.",
                    speech_text="The available knowledge is insufficient or conflicting. Please clarify.",
                    citations=[ctx.evidence[c] for c in dict.fromkeys(answer.citation_ids)],
                    reason_code="CUEKB_EVIDENCE_UNSAFE",
                )
            # Conservative policy: affirmative business answers need current evidence.
            if not answer.citation_ids and not ctx.cards:
                return AnswerBundle(
                    status="insufficient_evidence",
                    display_text="I could not find enough evidence. Please clarify or contact a representative.",
                    speech_text="I could not find enough evidence. Please clarify.",
                    reason_code="CUEKB_NO_EVIDENCE",
                )
            if answer.citation_ids and "search_knowledge" not in ctx.invoked:
                return self.failure(
                    "RAG_INVALID_CITATION", "No knowledge search was recorded for this request."
                )
        degraded = any(
            item.get("retrieval_status") == "degraded"
            or item.get("scope_limited")
            or item.get("application_limited")
            for item in ctx.retrievals
        )
        display_text = answer.display_text
        speech_text = answer.speech_text
        if status == "answered" and degraded:
            display_text = (
                "Note: Search scope was limited. The answer uses only currently available material.\n"
                + display_text
            )
            speech_text = ("Search scope was limited. " + speech_text)[:160]
            reason_code = "CUEKB_DEGRADED"
        if not printable_ascii(speech_text):
            speech_text = "Please read the written response in the portal."
            reason_code = "VOICE_NON_ASCII_SPEECH"
        return AnswerBundle(
            status=status,
            display_text=display_text,
            speech_text=speech_text,
            citations=[ctx.evidence[c] for c in dict.fromkeys(answer.citation_ids)],
            cards=ctx.cards,
            is_mock=any(c.is_mock for c in ctx.evidence.values()),
            reason_code=reason_code,
        )

    @staticmethod
    def failure(code, message):
        return AnswerBundle(status="failed", display_text=message, speech_text=message, reason_code=code)
