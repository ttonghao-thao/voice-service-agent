import asyncio
import json
import re

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
)
from app.config import ROOT
from app.contracts import AgentAnswer, AnswerBundle, now, printable_ascii
from openai import AsyncOpenAI


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
        if self.settings.agent_provider == "mock":
            if "search_knowledge" not in ctx.allowed_tools:
                return self.failure("AGENT_NO_AUTHORIZED_TOOL", "No authorized knowledge tool is enabled for this demonstration.")
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
            return self.failure("AGENT_NOT_CONFIGURED", "The text model is not configured for business answers.")
        tools = []
        for name in sorted(ctx.allowed_tools):
            spec = self.registry.specs[name]
            adapter = self.registry.adapters[spec.adapter_id]

            async def invoke(wrapper, args, tool_name=name):
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
            model_settings=ModelSettings(parallel_tool_calls=False),
        )
        inputs = history + [{"role": "user", "content": request}]
        stream = None
        try:
            async with asyncio.timeout(self.settings.agent_deadline_ms / 1000):
                if progress:
                    stream = Runner.run_streamed(
                        agent, inputs, context=ctx, max_turns=8, run_config=RunConfig(tracing_disabled=True)
                    )
                    async for _ in stream.stream_events():
                        pass  # Raw deltas and reasoning are never exposed before evidence validation.
                    raw = stream.final_output
                else:
                    result = await Runner.run(
                        agent, inputs, context=ctx, max_turns=8, run_config=RunConfig(tracing_disabled=True)
                    )
                    raw = result.final_output
            answer = AgentAnswer.model_validate(raw)
            return await self.validate(answer, ctx)
        except TimeoutError:
            return self.failure("AGENT_TIMEOUT", "The request timed out. Please try again later.")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not propagate provider exceptions: their payloads can contain private prompts and credentials.
            return self.failure("AGENT_FAILED", "The request failed. Please try again later.")
        finally:
            if stream is not None and not stream.is_complete:
                stream.cancel(mode="immediate")

    async def validate(self, answer, ctx):
        references = set(re.findall(r"\[(C\d+)\]", answer.display_text))
        if ctx.tool_errors and answer.status == "answered":
            return self.failure(ctx.tool_errors[-1], "The search failed, so I cannot provide a reliable answer right now.")
        if references - set(answer.citation_ids) or any(
            cid not in ctx.evidence for cid in answer.citation_ids
        ):
            return self.failure("RAG_INVALID_CITATION", "Source validation failed, so I cannot provide a reliable answer right now.")
        if not all(
            [
                await self.registry.store.enabled(name)
                and self.registry.deployment_enabled(name)
                and await self.registry.store.tool_revision(name) == ctx.tool_versions.get(name, 0)
                for name in ctx.invoked
            ]
        ):
            return self.failure("FORBIDDEN", "Knowledge access changed. Please ask again.")
        if answer.status == "answered":
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
                return self.failure("RAG_INVALID_CITATION", "No knowledge search was recorded for this request.")
        degraded = any(
            item.get("retrieval_status") == "degraded"
            or item.get("scope_limited")
            or item.get("application_limited")
            for item in ctx.retrievals
        )
        display_text = answer.display_text
        speech_text = answer.speech_text
        reason_code = None
        if answer.status == "answered" and degraded:
            display_text = "Note: Search scope was limited. The answer uses only currently available material.\n" + display_text
            speech_text = ("Search scope was limited. " + speech_text)[:160]
            reason_code = "CUEKB_DEGRADED"
        if not printable_ascii(speech_text):
            speech_text = "Please read the written response in the portal."
            reason_code = "VOICE_NON_ASCII_SPEECH"
        return AnswerBundle(
            status=answer.status,
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
