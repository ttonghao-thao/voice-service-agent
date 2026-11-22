from types import SimpleNamespace

import pytest
from app.config import Settings
from app.voice.delegation_router import DelegationRouter
from app.voice.sophon_relay import knowledge_task


def router(configured=True):
    registry = SimpleNamespace(settings=Settings(_env_file=None,
        cuekb_base_url="https://cuekb.fixture" if configured else "", cuekb_api_key="key" if configured else ""),
        specs={name: SimpleNamespace(description=name, permission_scope=name + ":read")
               for name in ("weather", "web_search")})
    return DelegationRouter(registry)


CATALOG = {"search_knowledge_base": SimpleNamespace(kind="cuekb", name="search_knowledge_base")}


@pytest.mark.parametrize("task,route,arguments", [
    ("WEATHER： 北京 | tomorrow", "WEATHER", {"place": "北京", "date": "tomorrow", "units": "metric"}),
    ("帮我查询北京明天的天气", "WEATHER", {"place": "北京", "date": "明天", "units": "metric"}),
    ("查询上海明天会下雨吗？", "WEATHER", {"place": "上海", "date": "明天", "units": "metric"}),
    ("Check weather in Melbourne tomorrow", "WEATHER", {"place": "Melbourne", "date": "tomorrow", "units": "metric"}),
    ("Check if it will rain in Melbourne tomorrow", "WEATHER", {"place": "Melbourne", "date": "tomorrow", "units": "metric"}),
    ("Check the weather forecast for Melbourne tomorrow", "WEATHER", {"place": "Melbourne", "date": "tomorrow", "units": "metric"}),
    ("查询中兴 F7005 的产品参数，不要旧版本", "CUEKB", {"query": "查询中兴 F7005 的产品参数，不要旧版本"}),
    ("今天有什么科技新闻？", "WEB_SEARCH", {"query": "今天有什么科技新闻？", "freshness": None}),
    ("检索最新开源全双工语音模型", "WEB_SEARCH", {"query": "检索最新开源全双工语音模型", "freshness": None}),
    ("WEB_SEARCH: latest AI news | pw", "WEB_SEARCH", {"query": "latest AI news", "freshness": "pw"}),
])
def test_native_task_normalization_preserves_parameters(task, route, arguments):
    result = router().parse(task, CATALOG)
    assert result.route == route and result.source == "delegation"
    assert result.parse_method == ("structured" if ":" in task or "：" in task else "rule")
    assert result.decision.arguments.model_dump() == arguments


@pytest.mark.parametrize("task,route", [
    ("今天天气怎么样？", "CLARIFY"), ("weather here tomorrow", "CLARIFY"),
    ("查询北京和上海明天和今天的天气", "CLARIFY"), ("查询北京下周天气", "CLARIFY"),
    ("查产品参数和天气", "CLARIFY"), ("帮我关客厅的灯", "UNSUPPORTED"),
    ("把客厅的灯关掉", "UNSUPPORTED"), ("用户上次说喜欢吃什么", "UNSUPPORTED"),
    ("查询北京和上海天气", "CLARIFY"), ("WEB_SEARCH: 13800138000", "CLARIFY"),
    ("你还记得我喜欢吃什么吗", "UNSUPPORTED"), ("What time is it?", "UNSUPPORTED"),
    ("WEB_SEARCH: customer SN 123456", "CLARIFY"), ("WEB_SEARCH: public internal logs", "CLARIFY"),
    ("WEB_SEARCH: search example@example.com", "CLARIFY"), ("WEB_SEARCH: account 138-1234-5678", "CLARIFY"),
    ("UNKNOWN: public news", "CLARIFY"), ("CUEKB:", "CLARIFY"), ("", "CLARIFY"),
])
def test_ambiguous_unsupported_and_sensitive_tasks_do_not_execute(task, route):
    result = router().parse(task, CATALOG)
    assert result.route == route and result.decision is None


@pytest.mark.parametrize("task,route", [
    ("CUEKB: 中兴 F7005 产品参数", "CLARIFY"),
    ("查询中兴 F7005 的产品参数", "CLARIFY"),
    ("CUEKB: 公开的中兴 F7005 产品手册", "WEB_SEARCH"),
    ("CUEKB: 公开的内部客户设备日志", "CLARIFY"),
])
def test_unconfigured_knowledge_requires_explicit_public_scope(task, route):
    assert router(False).parse(task, CATALOG).route == route


def test_ws_scope_does_not_expand_to_public_tools():
    assert knowledge_task("查询中兴 F7005 的产品参数") == "查询中兴 F7005 的产品参数"
    assert knowledge_task("CUEKB: exact model and version") == "exact model and version"
    assert knowledge_task("今天有什么科技新闻？") == ""
