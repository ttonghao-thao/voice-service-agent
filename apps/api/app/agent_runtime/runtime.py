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
from app.contracts import AgentAnswer, AnswerBundle, now
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
            result = await self.registry.invoke("search_knowledge", {"query": request}, ctx)
            citations = list(ctx.evidence.values())
            return AnswerBundle(
                status="answered" if citations else "insufficient_evidence",
                display_text=(
                    "【演示模式，未调用文本模型】"
                    + (
                        citations[0].content + " [C1]"
                        if citations
                        else "未查询到足够依据。可输入“联调示例”检查合成引用展示。"
                    )
                ),
                speech_text="这是合成联调结果，不代表真实业务答案。",
                citations=citations,
                is_mock=True,
                reason_code=None if result.get("hits") else "RAG_NO_EVIDENCE",
            )
        if not self.client or not self.settings.agent_model:
            return self.failure("AGENT_NOT_CONFIGURED", "文本模型尚未配置，无法进行业务推理。")
        tools = []
        for name in sorted(ctx.allowed_tools):
            spec = self.registry.specs[name]
            adapter = self.registry.adapters[spec.adapter_id]

            async def invoke(wrapper, args, tool_name=name):
                if progress:
                    await progress("正在查询" + self.registry.specs[tool_name].display_name)
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
            name="客服业务助手",
            instructions=self.prompt
            + "\n服务端当前 UTC 时间："
            + now().isoformat()
            + "\n已确认地点与查询条件（不包含可复用天气数值）："
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
            return self.failure("AGENT_TIMEOUT", "处理超时，请稍后重试。")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not propagate provider exceptions: their payloads can contain private prompts and credentials.
            return self.failure("AGENT_FAILED", "业务处理失败，请稍后重试。")
        finally:
            if stream is not None and not stream.is_complete:
                stream.cancel(mode="immediate")

    async def validate(self, answer, ctx):
        references = set(re.findall(r"\[(C\d+)\]", answer.display_text))
        if ctx.tool_errors and answer.status == "answered":
            return self.failure(ctx.tool_errors[-1], "查询未成功，暂时无法提供可靠答案。")
        if references - set(answer.citation_ids) or any(
            cid not in ctx.evidence for cid in answer.citation_ids
        ):
            return self.failure("RAG_INVALID_CITATION", "来源校验未通过，暂时无法提供可靠答案。")
        if not all(
            [
                await self.registry.store.enabled(name)
                and await self.registry.store.tool_revision(name) == ctx.tool_versions.get(name, 0)
                for name in ctx.invoked
            ]
        ):
            return self.failure("FORBIDDEN", "查询权限已变更，请重新提问。")
        if answer.status == "answered":
            # Conservative policy: all affirmative business answers need current evidence or structured weather.
            if not answer.citation_ids and not ctx.cards:
                return AnswerBundle(
                    status="insufficient_evidence",
                    display_text="未查询到足够依据，请补充信息或转交人工。",
                    speech_text="未查询到足够依据，请补充信息。",
                    reason_code="RAG_NO_EVIDENCE",
                )
            if answer.citation_ids and "search_knowledge" not in ctx.invoked:
                return self.failure("RAG_INVALID_CITATION", "本轮缺少知识检索记录。")
        return AnswerBundle(
            status=answer.status,
            display_text=answer.display_text,
            speech_text=answer.speech_text,
            citations=[ctx.evidence[c] for c in dict.fromkeys(answer.citation_ids)],
            cards=ctx.cards,
            is_mock=any(c.is_mock for c in ctx.evidence.values()),
        )

    @staticmethod
    def failure(code, message):
        return AnswerBundle(status="failed", display_text=message, speech_text=message, reason_code=code)
