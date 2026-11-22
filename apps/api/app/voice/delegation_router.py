"""Validate Sophon client tasks and choose one permitted read-only operation."""

import re
from dataclasses import dataclass
from functools import partial

from app.agent_runtime.dispatch import ExecutionDecision, NativeTool
from app.contracts import AnswerBundle, Citation, uid
from app.tools.schemas import WeatherInput
from app.tools.web_search import WebSearchInput
from app.voice.sophon_relay import SophonQuestion
from pydantic import ValidationError

INTERNAL = re.compile(r"内部|私有|保密|公司资料|企业资料|\b(?:internal|confidential|private|intranet|proprietary)\b", re.I)
SENSITIVE = re.compile(
    r"客户|日志|账号|帐号|密码|密钥|序列号|工单|个人|用户资料|记忆|"
    r"\b(?:customer|client data|logs?|account|password|secret|token|serial|sn|email|memory|profile)\b|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b1[3-9]\d{9}\b|\b\d{3}(?:[.\- ]\d{3,4}){2,}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", re.I)
PUBLIC = re.compile(r"公开|互联网|公网|\b(?:public|internet|web)\b", re.I)
WEATHER = re.compile(r"天气|气温|降雨|下雨|预报|\b(?:weather|temperature|rain|forecast)\b", re.I)
KNOWLEDGE = re.compile(r"内部|产品|设备|型号|手册|配置|运维|参数|\b(?:internal|product|device|manual|configuration|specifications?|specs?)\b", re.I)
WEB = re.compile(r"新闻|最新|开源|百科|公开|互联网|检索|\b(?:news|latest|open.source|encyclopedia|public|web search|search the web)\b", re.I)
ACTION = re.compile(
    r"(?:(?<!公)开(?!源)|关(?!键)|控制).*(?:灯|空调|设备)|(?:灯|空调).*(?:开|关)|记得|记忆|几点|现在.*时间|"
    r"用户.*(?:上次|之前|以前).*(?:喜欢|偏好|说)|"
    r"\b(?:turn (?:on|off)|remember|memory|what time|current time)\b", re.I)


@dataclass(frozen=True)
class RouteResult:
    route: str
    decision: ExecutionDecision | None = None
    question: str = ""
    parse_method: str = "rule"
    source: str = "delegation"
    reason: str = ""


def natural_task(task):
    """Only parse the native task. No ASR, output text or user location is used."""
    if ACTION.search(task):
        return "UNSUPPORTED", task
    matches = [name for name, pattern in (("WEATHER", WEATHER), ("CUEKB", KNOWLEDGE), ("WEB_SEARCH", WEB))
               if pattern.search(task)]
    # Explicit public product information can use web; an internal product
    # request remains private even if it mentions latest/public material too.
    if matches == ["CUEKB", "WEB_SEARCH"] and PUBLIC.search(task) and not INTERNAL.search(task):
        return "WEB_SEARCH", task
    if len(matches) != 1:
        return "CLARIFY", task
    route = matches[0]
    if route != "WEATHER":
        return route, task
    dates = re.findall(r"\d{4}-\d{2}-\d{2}|今天|明天|today|tomorrow", task, re.I)
    if len(set(date.lower() for date in dates)) > 1 or re.search(r"后天|下周|周末|next week|weekend", task, re.I):
        return "CLARIFY", task
    date = dates[0].lower() if dates else "today"
    place = re.sub(r"\d{4}-\d{2}-\d{2}|今天|明天|\b(?:today|tomorrow)\b", "", task, flags=re.I)
    place = re.sub(r"^(?:请|帮我|帮忙|麻烦|查一下|查询|查查|看看|查|一下)+", "", place).strip()
    if re.search(r"[\u4e00-\u9fff]", place):
        place = re.sub(r"(?:的)?(?:天气|气温|降雨|下雨|天气预报|预报).*$", "", place)
        place = re.sub(r"(?:会不会|是否会|会|是否)$", "", place).strip(" ，。？！?的")
    else:
        place = re.sub(r"^(?:please\s+)?(?:check|look up|find|tell me|what(?:'s| is))\s+", "", place, flags=re.I)
        place = re.sub(r"^(?:the\s+)?(?:weather(?:\s+forecast)?|temperature|forecast)(?:\s+(?:in|for|at))?\s*", "", place, flags=re.I)
        place = re.sub(r"^(?:is it going to rain|will it rain|(?:if|whether) it will rain)\s+in\s+", "", place, flags=re.I)
        place = place.strip(" .,?!")
    # Unknown deixis or remaining grammar is not a city. Do not let GeoAPI
    # select an arbitrary place for an underspecified request.
    if not place or re.search(r"这里|当地|我这|怎么样|什么|和|还是|以及|、|\b(?:here|there|my|weather|rain|in|for|and|or)\b", place, re.I):
        return "CLARIFY", task
    return route, f"{place} | {date}"


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
        result = self.parse(task, catalog)
        return result.decision, result.question

    def parse(self, task, catalog):
        if not isinstance(task, str) or not task.strip() or len(task) > 2000:
            return RouteResult("CLARIFY", reason="invalid_task")
        if any(not char.isprintable() and char not in "\r\n\t" for char in task):
            return RouteResult("CLARIFY", reason="invalid_task")
        route, sep, arguments = task.replace("：", ":", 1).partition(":")
        route, arguments = route.strip().upper(), arguments.strip()
        structured = bool(sep and (route in {"WEATHER", "CUEKB", "WEB_SEARCH", "CLARIFY", "UNSUPPORTED"}
                                  or re.fullmatch(r"[A-Z_]{2,40}", route)))
        method = "structured" if structured else "rule"
        if not structured:
            route, arguments = natural_task(task.strip())
        if route in {"CLARIFY", "UNSUPPORTED"}:
            return RouteResult(route, parse_method=method)
        if not arguments:
            return RouteResult("CLARIFY", parse_method=method, reason="missing_arguments")
        if route == "CUEKB":
            settings = self.registry.settings
            if not settings.cuekb_base_url and not settings.cuekb_api_key.get_secret_value():
                if not PUBLIC.search(arguments) or INTERNAL.search(arguments) or SENSITIVE.search(arguments):
                    return RouteResult("CLARIFY", parse_method=method, reason="public_scope_unconfirmed")
                route = "WEB_SEARCH"
            else:
                tool = catalog.get("search_knowledge_base")
                if not tool or tool.kind != "cuekb":
                    return RouteResult("CLARIFY", parse_method=method, reason="knowledge_unavailable")
                if len(arguments) > 2000 or not any(char.isalnum() for char in arguments):
                    return RouteResult("CLARIFY", parse_method=method, reason="invalid_arguments")
                return RouteResult(route, ExecutionDecision(tool, SophonQuestion(query=arguments)), arguments, method)
        tool = self.tools.get(route)
        if tool is None or INTERNAL.search(arguments) or SENSITIVE.search(arguments):
            return RouteResult("CLARIFY", parse_method=method, reason="unsafe_or_unknown_task")
        argument, _, qualifier = arguments.partition("|")
        if not any(char.isalnum() for char in argument):
            return RouteResult("CLARIFY", parse_method=method, reason="invalid_arguments")
        try:
            if route == "WEATHER":
                parsed = WeatherInput(place=argument.strip(), date=qualifier.strip() or "today")
            else:
                parsed = WebSearchInput(query=argument.strip(), freshness=qualifier.strip() or None)
        except ValidationError:
            return RouteResult("CLARIFY", parse_method=method, reason="invalid_arguments")
        return RouteResult(route, ExecutionDecision(tool, parsed), arguments, method)

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
