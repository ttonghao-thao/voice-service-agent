import asyncio
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from app.contracts import Citation, DomainError, now, uid
from app.tools.schemas import (
    PlacesResponse,
    RagInput,
    RagResponse,
    RagToolResult,
    WeatherClarification,
    WeatherInput,
    WeatherResponse,
)
from pydantic import TypeAdapter


async def bounded_json(client, method, url, *, limit=32768, **kwargs):
    # Redirects are intentionally disabled: configured endpoints cannot redirect into arbitrary networks.
    for attempt in range(2):
        try:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code >= 500 and attempt == 0:
                    await response.aclose()
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code >= 400:
                    raise DomainError("TOOL_UNAVAILABLE", "查询服务暂不可用", 502, True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise DomainError("TOOL_BAD_RESPONSE", "查询结果超过允许大小", 502)
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise DomainError("TOOL_BAD_RESPONSE", "查询服务返回格式错误", 502) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 1:
                raise DomainError("TOOL_UNAVAILABLE", "查询服务连接失败", 502, True) from exc
            await asyncio.sleep(0.1)
    raise DomainError("TOOL_UNAVAILABLE", "查询服务不可用", 502)


class RagAdapter:
    input_model = RagInput
    output_adapter = TypeAdapter(RagToolResult)

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def invoke(self, args, ctx):
        if not ctx.principal.knowledge_base_ids:
            raise DomainError("FORBIDDEN", "没有获授权的知识库", 403)
        request_id = uid()
        if self.settings.rag_mode == "mock":
            # No fabricated policy. Only this explicitly named synthetic fixture can produce a hit.
            data = {"request_id": request_id, "retrieval_id": uid(), "status": "ok", "hits": []}
            if "联调示例" in args.query:
                data["hits"] = [
                    {
                        "document_id": "synthetic",
                        "chunk_id": "fixture-1",
                        "title": "合成联调资料（非业务政策）",
                        "content": "这是一条合成联调资料，用于验证中文、引用与门户展示，不代表真实业务规则。",
                        "score": 1.0,
                        "source_uri": None,
                        "version": "synthetic-v1",
                        "updated_at": "2026-09-05T00:00:00Z",
                        "metadata": {},
                    }
                ]
        else:
            if not self.settings.rag_base_url or not self.settings.rag_api_key.get_secret_value():
                raise DomainError("TOOL_NOT_CONFIGURED", "知识库尚未配置", 503)
            data = await bounded_json(
                self.client,
                "POST",
                self.settings.rag_base_url.rstrip("/") + "/v1/retrieve",
                headers={
                    "Authorization": "Bearer " + self.settings.rag_api_key.get_secret_value(),
                    "X-Tenant-ID": ctx.principal.tenant_id,
                    "X-User-ID": ctx.principal.user_id,
                },
                json={
                    "request_id": request_id,
                    "query": args.query,
                    "knowledge_base_ids": list(ctx.principal.knowledge_base_ids),
                    "top_k": 5,
                    "locale": ctx.locale,
                    "filters": {},
                },
            )
        result = RagResponse.model_validate(data)
        if result.request_id != request_id:
            raise DomainError("TOOL_BAD_RESPONSE", "知识库请求关联不匹配", 502)
        versions = {}
        for hit in result.hits:
            versions.setdefault(hit.document_id, set()).add(hit.version)
        if result.status == "conflict" or any(len(v) > 1 for v in versions.values()):
            return {"status": "conflict", "hits": [], "retrieval_id": result.retrieval_id}
        budget = 6000
        hits = []
        for hit in result.hits:
            if len(hit.content) > budget:
                break
            budget -= len(hit.content)
            source = urlparse(hit.source_uri or "")
            allowed = {h.strip() for h in self.settings.rag_source_hosts.split(",") if h.strip()}
            uri = (
                hit.source_uri
                if source.scheme == "https" and source.hostname in allowed and not source.username
                else None
            )
            citation_id = f"C{len(ctx.evidence) + 1}"
            citation = Citation(
                citation_id=citation_id,
                **hit.model_dump(exclude={"score", "metadata", "source_uri"}),
                source_uri=uri,
                retrieval_id=result.retrieval_id,
                is_mock=self.settings.rag_mode == "mock",
            )
            ctx.evidence[citation_id] = citation
            hits.append(citation.model_dump())
        return {
            "status": "ok" if hits else "insufficient_evidence",
            "hits": hits,
            "retrieval_id": result.retrieval_id,
        }


class WeatherAdapter:
    input_model = WeatherInput
    output_adapter = TypeAdapter(WeatherResponse | WeatherClarification)

    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.cache = {}

    async def invoke(self, args, ctx):
        if self.settings.weather_mode == "mock":
            return {
                "status": "needs_clarification",
                "message": "演示环境未接入真实天气。天气合成数据仅用于契约测试。",
                "is_mock": True,
            }
        if not self.settings.weather_base_url or not self.settings.weather_api_key.get_secret_value():
            raise DomainError("TOOL_NOT_CONFIGURED", "天气供应商尚未配置", 503)
        base = self.settings.weather_base_url.rstrip("/")
        headers = {"Authorization": "Bearer " + self.settings.weather_api_key.get_secret_value()}
        places = PlacesResponse.model_validate(
            await bounded_json(
                self.client,
                "POST",
                base + "/v1/places/resolve",
                headers=headers,
                json={"query": args.place, "locale": ctx.locale},
            )
        )
        if len(places.places) != 1:
            return {
                "status": "needs_clarification",
                "places": [p.model_dump() for p in places.places],
                "message": "请确认城市、国家或地区。",
            }
        place = places.places[0]
        try:
            local_today = now().astimezone(ZoneInfo(place.timezone)).date()
            if args.date in (None, "今天", "today"):
                target = local_today
            elif args.date in ("明天", "tomorrow"):
                target = local_today + timedelta(days=1)
            else:
                target = date.fromisoformat(args.date)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise DomainError("TOOL_BAD_ARGUMENTS", "请提供明确日期或有效地点时区", 422) from exc
        key = (
            ctx.principal.tenant_id,
            place.id,
            target.isoformat(),
            args.units,
            self.settings.weather_provider,
        )
        cached = self.cache.get(key)
        if cached and (now() - cached[0]).total_seconds() < 300:
            card = cached[1]
        else:
            result = WeatherResponse.model_validate(
                await bounded_json(
                    self.client,
                    "POST",
                    base + "/v1/weather",
                    headers=headers,
                    json={"place_id": place.id, "date": target.isoformat(), "units": args.units},
                )
            )
            if (
                result.place != place
                or result.source.is_mock
                or result.temperature.unit != ("C" if args.units == "metric" else "F")
            ):
                raise DomainError("TOOL_BAD_RESPONSE", "天气地点、来源或单位不匹配", 502)
            try:
                valid = datetime.fromisoformat(result.valid_at.replace("Z", "+00:00"))
                fetched = datetime.fromisoformat(result.fetched_at.replace("Z", "+00:00"))
                if (
                    not valid.tzinfo
                    or not fetched.tzinfo
                    or valid.astimezone(ZoneInfo(place.timezone)).date() != target
                ):
                    raise ValueError("Date mismatch")
                if target != local_today and result.kind != "forecast":
                    raise ValueError("Forecast required")
                if (now() - fetched).total_seconds() > 300:
                    result.stale = True
            except ValueError as exc:
                raise DomainError("TOOL_BAD_RESPONSE", "天气有效时间不匹配", 502) from exc
            card = result.model_dump()
            if len(self.cache) >= 512:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (now(), card)
        ctx.slots["weather_place"] = card["place"]
        ctx.slots["weather_units"] = args.units
        ctx.slots["weather_last_date"] = target.isoformat()
        ctx.cards.append({"type": "weather", **card})
        return card
