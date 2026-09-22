import asyncio
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from app.contracts import Citation, DomainError, now, uid
from app.tools.schemas import (
    CueKBSearchInput,
    CueKBSearchRequest,
    CueKBSearchResponse,
    CueKBToolResult,
    PlacesResponse,
    WeatherClarification,
    WeatherInput,
    WeatherResponse,
)
from pydantic import TypeAdapter


async def bounded_json(client, method, url, *, limit=32768, error_map=None, **kwargs):
    # Redirects are intentionally disabled: configured endpoints cannot redirect into arbitrary networks.
    for attempt in range(2):
        try:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code >= 500 and attempt == 0:
                    await response.aclose()
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code >= 400:
                    if error_map and response.status_code in error_map:
                        code, message, status, retryable = error_map[response.status_code]
                        raise DomainError(code, message, status, retryable)
                    raise DomainError("TOOL_UNAVAILABLE", "Search service is temporarily unavailable", 502, True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise DomainError("TOOL_BAD_RESPONSE", "Search result exceeds the size limit", 502)
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise DomainError("TOOL_BAD_RESPONSE", "Search service returned an invalid response", 502) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 1:
                raise DomainError("TOOL_UNAVAILABLE", "Search service connection failed", 502, True) from exc
            await asyncio.sleep(0.1)
    raise DomainError("TOOL_UNAVAILABLE", "Search service unavailable", 502)


class CueKBAdapter:
    input_model = CueKBSearchInput
    output_adapter = TypeAdapter(CueKBToolResult)
    # Keep the model-facing result below ToolSpec.result_limit (32 KiB). CueKB's
    # source response can be larger because context is also kept per chunk.
    result_budget_bytes = 30000

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def invoke(self, args, ctx):
        if not ctx.principal.knowledge_base_ids:
            raise DomainError("FORBIDDEN", "No authorized knowledge base is available", 403)
        if self.settings.cuekb_mode == "mock":
            # No fabricated policy. Only this explicitly named synthetic fixture can produce a hit.
            trace_id = uid()
            data = {
                "trace_id": trace_id,
                "retrieval_status": "not_found",
                "evidence_status": "unassessed",
                "degraded_reasons": [],
                "scope_limited": False,
                "content_revisions": {kb: 1 for kb in ctx.principal.knowledge_base_ids},
                "timings_ms": {"total": 0.1},
                "retrieval_path": "synthetic",
                "executed_stages": ["synthetic_fixture"],
                "skipped_stages": [],
                "hits": [],
            }
            if "integration sample" in args.query.lower() or "联调示例" in args.query:
                data["retrieval_status"] = "ok"
                data["hits"] = [
                    {
                        "document_id": "00000000-0000-4000-8000-000000000101",
                        "chunk_id": "00000000-0000-4000-8000-000000000102",
                        "version_id": "00000000-0000-4000-8000-000000000103",
                        "rank": 1,
                        "source_text": "This synthetic integration excerpt verifies citations and portal display. It is not a real business rule.",
                        "context": None,
                        "title_path": ["Synthetic integration excerpt (not policy)"],
                        "anchor": {"page": 1, "heading_path": ["Synthetic integration excerpt"]},
                        "metadata": {"business_version": "synthetic-v1"},
                        "retrieval_sources": ["synthetic_fixture"],
                    }
                ]
        else:
            if not self.settings.cuekb_base_url or not self.settings.cuekb_api_key.get_secret_value():
                raise DomainError("TOOL_NOT_CONFIGURED", "Knowledge base is not configured", 503)
            body = CueKBSearchRequest(
                query=args.query,
                kb_ids=list(ctx.principal.knowledge_base_ids),
                mode=self.settings.cuekb_search_mode,
                top_k=self.settings.cuekb_top_k,
                filters={
                    "product_model": args.product_model,
                    "software_version": args.software_version,
                },
                include_context=True,
            )
            data = await bounded_json(
                self.client,
                "POST",
                self.settings.cuekb_base_url.rstrip("/") + "/v1/search",
                headers={
                    "Authorization": "Bearer " + self.settings.cuekb_api_key.get_secret_value(),
                },
                json=body.model_dump(mode="json"),
                limit=262144,
                error_map={
                    401: ("CUEKB_AUTH_FAILED", "Knowledge service authentication failed", 502, False),
                    403: ("CUEKB_FORBIDDEN", "Knowledge service cannot access the authorized scope", 502, False),
                    422: ("CUEKB_CONTRACT_ERROR", "Knowledge query does not match the service contract", 502, False),
                    429: ("CUEKB_RATE_LIMITED", "Knowledge service is busy. Please try again later.", 503, True),
                },
            )
        result = CueKBSearchResponse.model_validate(data)
        trace_id = str(result.trace_id)
        content_revisions = {str(key): value for key, value in result.content_revisions.items()}
        budget = 6000
        hits = []
        accepted_ids = []
        omitted = 0
        context_omitted_any = False
        base = {
            "status": result.retrieval_status,
            "evidence_status": result.evidence_status,
            "trace_id": trace_id,
            "retrieval_id": trace_id,
            "degraded_reasons": result.degraded_reasons,
            "scope_limited": result.scope_limited,
            "content_revisions": content_revisions,
            "timings_ms": result.timings_ms,
            "retrieval_path": result.retrieval_path,
            "executed_stages": result.executed_stages,
            "skipped_stages": result.skipped_stages,
        }

        def fits(candidate):
            return (
                len(
                    json.dumps(
                        {**base, "hits": [*hits, candidate], "hits_omitted": len(result.hits)}
                    ).encode()
                )
                <= self.result_budget_bytes
            )

        for hit in result.hits:
            content = hit.source_text
            context = hit.context if hit.context and hit.context != content else None
            if len(content) > budget:
                omitted += 1
                continue
            context_omitted = bool(hit.context and context is None and hit.context_parts)
            if len(content) + len(context or "") > budget:
                context = None
                context_omitted = True
            citation_id = f"C{len(ctx.evidence) + 1}"
            title = " / ".join(part for part in hit.title_path if part.strip()) or "Source excerpt"
            safe_metadata = {
                key: value
                for key, value in hit.metadata.items()
                if key in {"business_version", "product_model", "software_version"}
                and isinstance(value, (str, int, bool))
            }
            business_version = safe_metadata.get("business_version")
            if not isinstance(business_version, str):
                business_version = None
            citation = Citation(
                citation_id=citation_id,
                document_id=str(hit.document_id),
                chunk_id=str(hit.chunk_id),
                title=title,
                version_id=str(hit.version_id),
                business_version=business_version,
                content=content,
                context=context,
                context_parts=[part.model_dump(mode="json") for part in hit.context_parts] if context else [],
                context_truncated=hit.context_truncated,
                context_omitted=context_omitted,
                relations=[relation.model_dump(mode="json") for relation in hit.relations],
                trace_id=trace_id,
                retrieval_id=trace_id,
                retrieval_status=result.retrieval_status,
                evidence_status=result.evidence_status,
                degraded_reasons=tuple(result.degraded_reasons),
                scope_limited=result.scope_limited,
                content_revisions=content_revisions,
                rank=hit.rank,
                title_path=tuple(hit.title_path),
                anchor=hit.anchor.model_dump(mode="json"),
                retrieval_sources=tuple(hit.retrieval_sources),
                metadata=safe_metadata,
                authorized_kb_ids=tuple(sorted(ctx.principal.knowledge_base_ids)),
                is_mock=self.settings.cuekb_mode == "mock",
            )
            candidate = citation.model_dump(mode="json")
            if not fits(candidate) and context:
                citation.context = None
                citation.context_parts = []
                citation.context_omitted = True
                candidate = citation.model_dump(mode="json")
            if not fits(candidate):
                omitted += 1
                continue
            budget -= len(content) + len(citation.context or "")
            context_omitted_any = context_omitted_any or citation.context_omitted
            ctx.evidence[citation_id] = citation
            hits.append(candidate)
            accepted_ids.append(citation_id)
        if omitted:
            for citation_id, candidate in zip(accepted_ids, hits, strict=True):
                ctx.evidence[citation_id].hits_omitted = omitted
                candidate["hits_omitted"] = omitted
        ctx.retrievals.append(
            {
                "trace_id": trace_id,
                "retrieval_status": result.retrieval_status,
                "evidence_status": result.evidence_status,
                "degraded_reasons": result.degraded_reasons,
                "scope_limited": result.scope_limited,
                "application_limited": bool(omitted or context_omitted_any),
            }
        )
        return {**base, "hits": hits, "hits_omitted": omitted}


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
