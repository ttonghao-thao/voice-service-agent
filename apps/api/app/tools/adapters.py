import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID
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

logger = logging.getLogger(__name__)


async def bounded_json(client, method, url, *, limit=32768, error_map=None, retry=True, on_error=None, **kwargs):
    # Redirects are intentionally disabled: configured endpoints cannot redirect into arbitrary networks.
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            async with client.stream(method, url, follow_redirects=False, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    raise DomainError("TOOL_BAD_RESPONSE", "Search service returned an unexpected redirect", 502)
                if response.status_code >= 400 and on_error is not None:
                    on_error(response)
                if response.status_code >= 500 and attempt + 1 < attempts:
                    await response.aclose()
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code >= 400:
                    if error_map and response.status_code in error_map:
                        mapped = error_map[response.status_code]
                        if callable(mapped):
                            mapped = await mapped(response)
                        code, message, status, retryable = mapped
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
        except httpx.DecodingError as exc:
            raise DomainError("TOOL_BAD_RESPONSE", "Search service returned invalid encoded data", 502) from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            if attempt + 1 == attempts:
                raise DomainError("TOOL_UNAVAILABLE", "Search service connection failed", 502, True) from exc
            await asyncio.sleep(0.1)
    raise DomainError("TOOL_UNAVAILABLE", "Search service unavailable", 502)


class CueKBAdapter:
    input_model = CueKBSearchInput
    output_adapter = TypeAdapter(CueKBToolResult)
    # HTTP evidence and the compact native speech packet have separate budgets.
    # Preserve complete evidence groups here; the native consumer chooses which
    # authorized groups fit its smaller function-channel payload.
    result_budget_bytes = 30000

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    @staticmethod
    def _mock_response(ctx, query):
        """An explicit synthetic V2 result; never pretend this is a product fact."""
        hits = []
        if "integration sample" in query.lower() or "联调示例" in query:
            hits = [{
                "document_id": "00000000-0000-4000-8000-000000000101",
                "chunk_id": "00000000-0000-4000-8000-000000000102",
                "version_id": "00000000-0000-4000-8000-000000000103",
                "rank": 1, "score": 1 / 61, "score_type": "rrf",
                "source_text": "This synthetic integration excerpt verifies citations and portal display. It is not a real business rule.",
                "title_path": ["Synthetic integration excerpt (not policy)"],
                "anchor": {"page": 1, "heading_path": ["Synthetic integration excerpt"]},
                "metadata": {"business_version": "synthetic-v2"},
                "retrieval_sources": ["synthetic_fixture"],
                "processing_profile": "evidence-v2:synthetic-fixture",
                "context_state": "disabled", "applicability": {"status": "unknown"},
            }]
        return {
            "schema_version": "2", "trace_id": uid(),
            "retrieval_status": "ok" if hits else "not_found",
            "evidence_status": "unassessed", "degraded_reasons": [],
            "scope_limited": False,
            "content_revisions": {kb: 1 for kb in ctx.principal.knowledge_base_ids},
            "timings_ms": {"total": 0.1}, "retrieval_path": "synthetic",
            "executed_stages": ["synthetic_fixture"], "skipped_stages": [],
            "ranking_method": "keyword_rrf", "ranking_version": "sha256:" + "0" * 64,
            "diagnostics": {
                "counts": {"keyword": len(hits), "union": len(hits),
                           "authoritative": len(hits), "returned": len(hits)},
                "reranker": {"configured": False, "executed": False,
                             "skipped_reason": "reranker_service_unconfigured"},
                "keyword_limit": 1, "vector_limit": 0, "relation_limit": 0,
            },
            "hits": hits,
        }

    @staticmethod
    async def _configuration_error(response):
        # Public V2 error codes distinguish an old projection from a temporary
        # configuration revision. Read only a small error envelope; never return
        # arbitrary upstream detail or credentials to the caller.
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 4096:
                break
        known = {
            "INDEX_UPGRADE_REQUIRED": ("CUEKB_INDEX_UPGRADE_REQUIRED", "Knowledge search index must be upgraded for V2."),
            "SEARCH_CONFIGURATION_CHANGED": ("CUEKB_CONFIGURATION_CHANGED", "Knowledge search configuration changed. Start a new request."),
            "INDEX_MAINTENANCE_IN_PROGRESS": ("CUEKB_INDEX_MAINTENANCE", "Knowledge search index maintenance is in progress."),
        }
        try:
            payload = json.loads(body)
            code = payload.get("error_code") if isinstance(payload, dict) else None
            if not isinstance(code, str):
                code = None
        except (ValueError, UnicodeError):
            code = None
        local_code, message = known.get(code, (
            "CUEKB_CONFIGURATION_ERROR", "Knowledge service configuration or index needs attention."))
        return local_code, message, 502, False

    @staticmethod
    def _log_http_error(response, ctx):
        # Only accept the server UUID as correlation data. Do not echo provider
        # bodies, URLs, API keys or arbitrary header text into diagnostics.
        try:
            trace_id = str(UUID(response.headers.get("X-Trace-ID", "")))
        except (ValueError, TypeError, AttributeError):
            trace_id = "-"
        logger.info("cuekb_http_error conversation_id=%s turn_id=%s status=%s trace_id=%s",
                    ctx.conversation_id, ctx.turn_id, response.status_code, trace_id)

    async def invoke(self, args, ctx):
        started = time.monotonic()
        if not ctx.principal.knowledge_base_ids:
            raise DomainError("FORBIDDEN", "No authorized knowledge base is available", 403)
        if self.settings.cuekb_mode == "mock":
            data = self._mock_response(ctx, args.query)
        else:
            if not self.settings.cuekb_base_url or not self.settings.cuekb_api_key.get_secret_value():
                raise DomainError("TOOL_NOT_CONFIGURED", "Knowledge base is not configured", 503)
            endpoint = self.settings.cuekb_base_url.rstrip("/") + "/v2/search"
            parsed_endpoint = urlparse(endpoint)
            endpoint_host = parsed_endpoint.hostname or "unknown"
            if parsed_endpoint.port:
                endpoint_host += f":{parsed_endpoint.port}"
            logger.info(
                "cuekb_request_started conversation_id=%s turn_id=%s endpoint=%s://%s%s kb_count=%s",
                ctx.conversation_id, ctx.turn_id, parsed_endpoint.scheme,
                endpoint_host, parsed_endpoint.path, len(ctx.principal.knowledge_base_ids),
            )
            body = CueKBSearchRequest(
                query=args.query, kb_ids=list(ctx.principal.knowledge_base_ids),
                mode=self.settings.cuekb_search_mode, top_k=self.settings.cuekb_top_k,
                filters={"product_model": args.product_model, "software_version": args.software_version},
                include_context=True, include_diagnostics=False,
                max_evidence_bytes=self.settings.cuekb_max_evidence_bytes,
            )
            data = await bounded_json(
                self.client, "POST", endpoint,
                headers={"Authorization": "Bearer " + self.settings.cuekb_api_key.get_secret_value()},
                json=body.model_dump(mode="json"), limit=262144,
                # One native input budget also bounds actual HTTP attempts.
                retry=not ctx.native_voice_search,
                on_error=lambda response: self._log_http_error(response, ctx),
                error_map={
                    401: ("CUEKB_AUTH_FAILED", "Knowledge service authentication failed", 502, False),
                    403: ("CUEKB_FORBIDDEN", "Knowledge service cannot access the authorized scope", 502, False),
                    409: self._configuration_error,
                    422: ("CUEKB_CONTRACT_ERROR", "Knowledge query does not match the service contract", 502, False),
                    429: ("CUEKB_RATE_LIMITED", "Knowledge service is busy. Please try again later.", 503, True),
                    504: ("CUEKB_TIMEOUT", "Knowledge service exceeded its search time budget.", 502, True),
                },
            )
        result = CueKBSearchResponse.model_validate(data)
        trace_id = str(result.trace_id)
        logger.info(
            "cuekb_response_validated conversation_id=%s turn_id=%s trace_id=%s retrieval_status=%s hit_count=%s duration_ms=%s service_total_ms=%s",
            ctx.conversation_id, ctx.turn_id, trace_id, result.retrieval_status,
            len(result.hits), round((time.monotonic() - started) * 1000), result.timings_ms.get("total"),
        )
        content_revisions = {str(key): value for key, value in result.content_revisions.items()}
        hits, accepted_ids = [], []
        omissions = {"payload_budget": 0, "optional_context": 0}
        base = {
            "cuekb_schema_version": "2", "status": result.retrieval_status,
            "evidence_status": result.evidence_status, "trace_id": trace_id,
            "retrieval_id": trace_id, "degraded_reasons": result.degraded_reasons,
            "scope_limited": result.scope_limited, "content_revisions": content_revisions,
            "timings_ms": result.timings_ms, "retrieval_path": result.retrieval_path,
            "executed_stages": result.executed_stages, "skipped_stages": result.skipped_stages,
            "ranking_method": result.ranking_method, "ranking_version": result.ranking_version,
            "diagnostics": result.diagnostics.model_dump(mode="json"),
            "adapter_omissions": omissions,
        }

        def fits(candidate):
            return len(json.dumps({**base, "hits": [*hits, candidate],
                                   "hits_omitted": len(result.hits)}).encode()) <= self.result_budget_bytes

        if len(json.dumps({**base, "hits": [], "hits_omitted": len(result.hits)}).encode()) > self.result_budget_bytes:
            raise DomainError("TOOL_BAD_RESPONSE", "Knowledge response metadata exceeds the result budget.", 502)

        for hit in result.hits:
            citation_id = f"C{len(ctx.evidence) + 1}"
            title = " / ".join(part for part in hit.title_path if part.strip()) or "Source excerpt"
            # Uploaded document tags are provenance, not proof of local fact
            # applicability. V2 subjects and conditions have their own contract.
            business_version = hit.metadata.get("business_version")
            if not isinstance(business_version, str):
                business_version = None
            citation = Citation(
                citation_id=citation_id, cuekb_schema_version="2",
                document_id=str(hit.document_id), chunk_id=str(hit.chunk_id),
                title=title, document_name=hit.document_name, content_sha256=hit.content_sha256,
                version_id=str(hit.version_id), business_version=business_version,
                content=hit.source_text,
                context=hit.context if hit.context and hit.context != hit.source_text else None,
                context_parts=[part.model_dump(mode="json") for part in hit.context_parts],
                context_truncated=hit.context_truncated, context_state=hit.context_state,
                context_omissions=list(hit.context_omissions),
                applicability=hit.applicability, source_spans=hit.source_spans,
                fact_kind=hit.fact_kind, dependency_issues=hit.dependency_issues,
                processing_profile=hit.processing_profile, source_unit_id=hit.source_unit_id,
                block_kind=hit.block_kind,
                score=hit.score, score_type=hit.score_type, stage_scores=hit.stage_scores,
                ranking_method=result.ranking_method, ranking_version=result.ranking_version,
                relations=[relation.model_dump(mode="json") for relation in hit.relations],
                trace_id=trace_id, retrieval_id=trace_id, retrieval_status=result.retrieval_status,
                evidence_status=result.evidence_status, degraded_reasons=tuple(result.degraded_reasons),
                scope_limited=result.scope_limited, content_revisions=content_revisions,
                rank=hit.rank, title_path=tuple(hit.title_path),
                anchor=hit.anchor.model_dump(mode="json"), retrieval_sources=tuple(hit.retrieval_sources),
                metadata={"business_version": business_version} if business_version else {},
                authorized_kb_ids=tuple(sorted(ctx.principal.knowledge_base_ids)),
                is_mock=self.settings.cuekb_mode == "mock",
            )
            candidate = citation.model_dump(mode="json")
            if not fits(candidate):
                # Optional neighbors may be omitted, but the primary source and
                # every known necessary part stay together as one evidence group.
                necessary = [part for part in citation.context_parts
                             if part.get("required") or (
                                 str(part.get("chunk_id")) == citation.chunk_id
                                 and part.get("source_text") == citation.content)]
                if len(necessary) < len(citation.context_parts):
                    citation.context_parts = necessary
                    context = "\n\n".join(part["source_text"] for part in necessary)
                    citation.context = context if context and context != citation.content else None
                    citation.context_omitted = True
                    citation.context_truncated = True
                    if citation.context_state != "unresolved":
                        citation.context_state = "truncated"
                    citation.context_omissions = list(dict.fromkeys([*citation.context_omissions, "budget_omitted"]))
                    candidate = citation.model_dump(mode="json")
                    omissions["optional_context"] += 1
            if not fits(candidate):
                omissions["payload_budget"] += 1
                continue
            ctx.evidence[citation_id] = citation
            hits.append(candidate)
            accepted_ids.append(citation_id)
        omitted = omissions["payload_budget"]
        for citation_id, candidate in zip(accepted_ids, hits, strict=True):
            ctx.evidence[citation_id].hits_omitted = omitted
            candidate["hits_omitted"] = omitted
        ctx.retrievals.append({
            "trace_id": trace_id, "retrieval_status": result.retrieval_status,
            "evidence_status": result.evidence_status, "degraded_reasons": result.degraded_reasons,
            "scope_limited": result.scope_limited,
            "application_limited": bool(omitted or omissions["optional_context"]),
            "cuekb_schema_version": "2", "ranking_method": result.ranking_method,
        })
        output = {**base, "hits": hits, "hits_omitted": omitted}
        scope_counts = {status: sum(hit.applicability.status == status for hit in result.hits)
                        for status in ("resolved", "unknown", "ambiguous")}
        logger.info("cuekb_v2_diagnostic conversation_id=%s data=%s", ctx.conversation_id, json.dumps({
            "schema_version": "2", "ranking_method": result.ranking_method, "trace_id": trace_id,
            "service_hits": len(result.hits), "adapter_retained": len(hits), "adapter_omitted": omitted,
            "scope_counts": scope_counts,
            "required_parts": sum(part.required for hit in result.hits for part in hit.context_parts),
            "unresolved_groups": sum(hit.context_state == "unresolved" for hit in result.hits),
            "omission_counts": {**result.diagnostics.omissions.model_dump(), **omissions},
            "service_hits_bytes": result.diagnostics.sizes.hits_bytes,
            "adapter_bytes": len(json.dumps(output).encode()),
            "service_total_ms": result.timings_ms.get("total"),
            "reranker": {"configured": result.diagnostics.reranker.configured,
                         "executed": result.diagnostics.reranker.executed},
        }, separators=(",", ":")))
        return output


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
                "message": "Live weather is unavailable in this demo. Synthetic weather data is for contract tests only.",
                "is_mock": True,
            }
        if not self.settings.weather_base_url or not self.settings.weather_api_key.get_secret_value():
            raise DomainError("TOOL_NOT_CONFIGURED", "The weather provider is not configured", 503)
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
                "message": "Please confirm the city and country or region.",
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
            raise DomainError("TOOL_BAD_ARGUMENTS", "Provide an explicit date and a valid location timezone", 422) from exc
        key = (
            ctx.principal.user_id,
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
                raise DomainError("TOOL_BAD_RESPONSE", "Weather location, source, or units do not match", 502)
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
                raise DomainError("TOOL_BAD_RESPONSE", "The weather validity time does not match the requested date", 502) from exc
            card = result.model_dump()
            if len(self.cache) >= 512:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (now(), card)
        ctx.slots["weather_place"] = card["place"]
        ctx.slots["weather_units"] = args.units
        ctx.slots["weather_last_date"] = target.isoformat()
        ctx.cards.append({"type": "weather", **card})
        return card
