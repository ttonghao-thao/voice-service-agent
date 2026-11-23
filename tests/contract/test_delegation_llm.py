"""Trust boundaries for the model that parses an already delegated native task."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app.config import Settings
from app.voice.delegation_router import DelegationRouter

CATALOG = {"search_knowledge_base": SimpleNamespace(kind="cuekb", name="search_knowledge_base")}


def router(client, *, configured=True, timeout=1):
    settings = Settings(_env_file=None,
        delegation_router_provider="openai_compatible", delegation_router_base_url="https://intent.fixture/v1",
        delegation_router_api_key="test-intent-secret", delegation_router_model="test-model",
        delegation_router_timeout_seconds=timeout,
        cuekb_base_url="https://cuekb.fixture" if configured else "", cuekb_api_key="key" if configured else "")
    registry = SimpleNamespace(settings=settings, client=client,
        specs={name: SimpleNamespace(description=name, permission_scope=name + ":read")
               for name in ("weather", "web_search")})
    return DelegationRouter(registry)


def intent(route, **values):
    return {"route": route, "place": None, "date": None, "units": None, "query": None, "freshness": None, **values}


def response(output, finish="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish,
        "message": {"content": json.dumps(output)}}]})


@pytest.mark.parametrize("task,output,route,args", [
    ("不要查上海，只查北京明天气温", intent("WEATHER", place="北京", date="tomorrow", units="metric"),
     "WEATHER", {"place": "北京", "date": "tomorrow", "units": "metric"}),
    ("北京今天气温用华氏表示", intent("WEATHER", place="北京", date="today", units="imperial"),
     "WEATHER", {"place": "北京", "date": "today", "units": "imperial"}),
    ("今天科技新闻 | 保留原条件", intent("WEB_SEARCH", query="今天科技新闻 | 保留原条件", freshness="pd"),
     "WEB_SEARCH", {"query": "今天科技新闻 | 保留原条件", "freshness": "pd"}),
    ("F7005 V2 参数，不要 F705", intent("CUEKB", query="F7005 V2 参数，不要 F705"),
     "CUEKB", {"query": "F7005 V2 参数，不要 F705"}),
    ("缺少查询城市", intent("CLARIFY"), "CLARIFY", None),
    ("关掉客厅的灯", intent("UNSUPPORTED"), "UNSUPPORTED", None),
])
async def test_model_output_uses_only_native_task_and_validated_parameters(task, output, route, args, caplog):
    requests = []

    def backend(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://intent.fixture/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-intent-secret"
        assert body["messages"][1] == {"role": "user", "content": task}
        assert len(body["messages"]) == 2 and "tools" not in body and "tool_choice" not in body
        assert body["response_format"]["json_schema"]["strict"] is True
        return response(output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await router(client).parse_native(task, CATALOG)
    assert result.route == route and result.parse_method == "llm" and result.source == "delegation"
    assert (result.decision.arguments.model_dump() if result.decision else None) == args
    assert len(requests) == 1 and "test-intent-secret" not in caplog.text


@pytest.mark.parametrize("output", [
    intent("WEB_SEARCH", query="F705 V1 参数"),
    intent("WEB_SEARCH", query="F7005 V2 参数", knowledge_base_ids=["unauthorized"]),
    intent("SHELL", query="F7005 V2 参数"),
    intent("WEATHER", place="London", date="today"),
    intent("WEATHER", place="北京", date="today"),
    intent("WEATHER", place="北京", date="2030-01-01"),
    intent("WEATHER", place="北京", date="tomorrow", units="imperial"),
    intent("UNSUPPORTED", query="untrusted"),
])
async def test_changed_or_invented_parameters_never_create_execution(output):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(output))) as client:
        result = await router(client).parse_native("北京明天气温；F7005 V2 参数", CATALOG)
    assert result.route == "CLARIFY" and result.decision is None
    assert result.reason.startswith("llm_")


@pytest.mark.parametrize("kind", ["http", "redirect", "json", "shape", "truncated", "oversize", "timeout"])
async def test_model_failures_are_bounded_and_do_not_fall_back_to_rules(kind):
    calls = []

    async def backend(request):
        calls.append(request)
        if kind == "http":
            return httpx.Response(503)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.fixture"})
        if kind == "json":
            return httpx.Response(200, content=b"not json")
        if kind == "shape":
            return httpx.Response(200, json={"choices": ["invalid"]})
        if kind == "truncated":
            return response(intent("WEATHER", place="北京", date="tomorrow"), finish="length")
        if kind == "oversize":
            return httpx.Response(200, content=b" " * 16385)
        await asyncio.sleep(.5)
        return response(intent("WEATHER", place="北京", date="tomorrow"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
        result = await router(client, timeout=.1).parse_native("查询北京明天的天气", CATALOG)
    assert result.route == "CLARIFY" and result.decision is None and result.parse_method == "llm"
    assert result.reason.startswith("llm_") and len(calls) == 1


@pytest.mark.parametrize("task,private", [
    ("内部产品资料", False), ("查询客户 SN 123456 日志", False), ("公开新闻", True),
    ("", False), ("x" * 2001, False),
])
async def test_private_or_invalid_tasks_never_leave_for_external_llm(task, private):
    def forbidden(request):
        pytest.fail("Private or invalid native task was sent to the external intent service")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await router(client).parse_native(task, CATALOG, private_context=private)
    assert result.route == "CLARIFY" and result.decision is None


async def test_structured_native_task_keeps_fast_path_without_model_call():
    def forbidden(request):
        pytest.fail("Structured task must not require a second model")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await router(client).parse_native("WEATHER: Beijing | tomorrow", CATALOG)
    assert result.parse_method == "structured" and result.decision.arguments.place == "Beijing"


async def test_public_fallback_does_not_rewrite_query_or_bypass_knowledge_configuration():
    task = "公开 F7005 V2 手册 | 不要旧版本"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(intent("CUEKB", query=task)))) as client:
        parsed = await router(client, configured=False).parse_native(task, CATALOG)
        assert parsed.route == "WEB_SEARCH" and parsed.decision.arguments.query == task
        parsed = await router(client, configured=True).parse_native(task, {})
        assert parsed.route == "CLARIFY" and parsed.decision is None


@pytest.mark.parametrize("task,allowed", [
    ("F7005 V2 的产品参数，不要旧版本", False),
    ("公开 F7005 V2 的产品参数，不要旧版本", True),
    ("检索开源项目的配置文档", True),
])
async def test_model_cannot_reclassify_unconfirmed_professional_data_as_public(task, allowed):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(intent("WEB_SEARCH", query=task)))) as client:
        parsed = await router(client).parse_native(task, CATALOG)
    assert (parsed.route == "WEB_SEARCH") is allowed
    if not allowed:
        assert parsed.decision is None and parsed.reason == "public_scope_unconfirmed"
