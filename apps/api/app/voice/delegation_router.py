"""Validate Sophon client tasks and choose one permitted read-only operation."""

import re
from functools import partial

from app.agent_runtime.dispatch import ExecutionDecision, NativeTool
from app.contracts import AnswerBundle, Citation, uid
from app.tools.schemas import WeatherInput
from app.tools.web_search import WebSearchInput
from app.voice.sophon_relay import SophonQuestion
from pydantic import ValidationError

INTERNAL = re.compile(r"内部|私有|保密|公司资料|企业资料|\b(?:internal|confidential|private|intranet|proprietary)\b", re.I)


class DelegationRouter:
    def __init__(self, registry):
        self.registry = registry
        self.tools = {
            route: NativeTool(
                name, registry.specs[name].description, schema, partial(self.run_external, name),
                required_tools=frozenset({name}), permission_scope=registry.specs[name].permission_scope,
            )
            for route, name, schema in (
                ("WEATHER", "weather", WeatherInput), ("WEB_SEARCH", "web_search", WebSearchInput),
            )
        }

    def select(self, task, catalog):
        route, sep, arguments = task.replace("：", ":", 1).partition(":")
        route, arguments = route.strip().upper(), arguments.strip()
        if not sep or not arguments:
            return None, ""
        if route == "CUEKB":
            settings = self.registry.settings
            if not settings.cuekb_base_url and not settings.cuekb_api_key.get_secret_value():
                # Only an unconnected integration falls back. Configured private
                # service errors/denials must never trigger a public request.
                route = "WEB_SEARCH"
            else:
                tool = catalog.get("search_knowledge_base")
                if not tool or tool.kind != "cuekb":
                    return None, ""
                if len(arguments) > 2000 or not any(char.isalnum() for char in arguments):
                    return None, ""
                return ExecutionDecision(tool, SophonQuestion(query=arguments)), arguments
        tool = self.tools.get(route)
        if tool is None or INTERNAL.search(arguments):
            return None, ""
        argument, _, qualifier = arguments.partition("|")
        try:
            if route == "WEATHER":
                parsed = WeatherInput(place=argument.strip(), date=qualifier.strip() or "today")
            else:
                parsed = WebSearchInput(query=argument.strip(), freshness=qualifier.strip() or None)
        except ValidationError:
            return None, ""
        return ExecutionDecision(tool, parsed), arguments

    async def run_external(self, name, request, ctx, history, progress):
        output = await self.registry.invoke(name, ctx.tool_arguments, ctx)
        chinese = ctx.locale == "zh-CN"
        if output.get("status") == "failed":
            if output["code"] == "WEATHER_DATE_OUT_OF_RANGE":
                message = "日期超出预报范围，请查询今天至未来九天。" if chinese else "Please choose today or one of the next nine days."
            else:
                message = "查询暂时失败，请稍后再试。" if chinese else "The search failed. Please try again."
            return AnswerBundle(status="failed", reason_code=output["code"], speech_text="",
                display_text=message)
        if name == "weather":
            if output["status"] != "ok" or output["stale"]:
                return AnswerBundle(
                    status="needs_clarification" if output["status"] != "ok" else "failed",
                    display_text=("请确认城市和所在国家或地区。" if chinese else "Please confirm the city and country or region.")
                    if output["status"] != "ok" else ("天气数据已过期。" if chinese else "Weather data expired."),
                    speech_text="",
                )
            location = output["place"]["name"]
            if output.get("temperature_min") and output.get("temperature_max"):
                low, high = output["temperature_min"], output["temperature_max"]
                degrees = f'{low["value"]}–{high["value"]} °{low["unit"]}'
            else:
                temperature = output["temperature"]
                degrees = f'{temperature["value"]} °{temperature["unit"]}'
            content = f'{location}: {degrees}, {output["condition"]} ({output["valid_at"]}). Source: {output["source"]["provider"]}.'
            citations = []
        else:
            trace_id = uid()
            citations = [
                Citation(
                    citation_id=f"W{index}", document_id=item["url"], chunk_id=f"{trace_id}:{index}",
                    title=item["title"], source_uri=item["url"], content=item["excerpt"],
                    version_id="web search", trace_id=trace_id, retrieval_id=trace_id, rank=index,
                    retrieval_sources=("public_web",),
                )
                for index, item in enumerate(output["sources"], start=1)
            ]
            if not citations:
                return AnswerBundle(status="insufficient_evidence", speech_text="",
                    display_text="没有找到相关的公开来源。" if chinese else "No matching public sources were found.")
            content = "找到了公开来源。" if chinese else "Public search returned these sources."
        return AnswerBundle(
            status="answered", display_text=content, speech_text="", cards=ctx.cards, citations=citations,
            reason_code="VOICE_ANSWER_UNVERIFIED", answer_kind="general",
        )
