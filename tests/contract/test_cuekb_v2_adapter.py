"""V2 HTTP/selection checks with synthetic fixtures; no live knowledge service."""

import copy
import json
import logging
from pathlib import Path

import httpx
import pytest
from app.agent_runtime.context import RunContext
from app.contracts import DomainError, Principal
from app.tools.adapters import CueKBAdapter
from app.tools.schemas import CueKBSearchInput
from pydantic import SecretStr, ValidationError

FIXTURE = Path(__file__).parents[1] / "fixtures/cuekb-v2-response.json"
KB = "11111111-1111-4111-8111-111111111111"


def response():
    return json.loads(FIXTURE.read_text())


def context():
    return RunContext(Principal(user_id="test", scopes=frozenset({"knowledge:read"}),
                                knowledge_base_ids=(KB,)), "v2-conversation", "v2-turn", 0,
                      native_voice_search=False)


async def invoke(app, payload, ctx=None, *, budget=None):
    settings = app.state.settings
    settings.cuekb_mode, settings.cuekb_base_url = "real", "https://cuekb.fixture"
    settings.cuekb_api_key = SecretStr("private-test-key")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    ctx = ctx or context()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = CueKBAdapter(settings, client)
        if budget is not None:
            adapter.result_budget_bytes = budget
        result = await adapter.invoke(CueKBSearchInput(query="Product A voltage"), ctx)
    return result, ctx, requests


async def test_v2_preserves_scope_and_required_parts_without_promoting_document_tags(app, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    payload = response()
    hit = payload["hits"][0]
    # This label is uploaded provenance, not a local attribution override.
    hit["metadata"].update(product_model="OTHER-PRODUCT", software_version="v99", business_version="release-1")
    hit["source_text"] = "  Input voltage: 12 V. Do not exceed 30 C.\n"
    hit["context_parts"][0]["source_text"] = hit["source_text"]
    hit["context"] = hit["source_text"]
    with caplog.at_level("INFO"):
        result, ctx, requests = await invoke(app, payload)
    request = requests[0]
    body = json.loads(request.content)
    assert request.url.path == "/v2/search"
    assert body["kb_ids"] == [KB]
    assert body["max_evidence_bytes"] == app.state.settings.cuekb_max_evidence_bytes
    assert body["include_diagnostics"] is False
    assert request.headers["authorization"] == "Bearer private-test-key"
    citation = ctx.evidence["C1"]
    assert citation.content == hit["source_text"]
    assert citation.cuekb_schema_version == "2"
    assert citation.metadata == {"business_version": "release-1"}
    assert citation.applicability.subjects[0].display_name == "Product A"
    assert citation.applicability.subjects[0].product_model == "A"
    assert citation.context_parts[0]["required"] is True
    assert citation.context_parts[0]["source_spans"]
    assert citation.score == hit["score"] and citation.score_type == "rrf"
    assert result["ranking_method"] == payload["ranking_method"]
    assert result["ranking_version"] == payload["ranking_version"]
    assert "cuekb_v2_diagnostic" in caplog.text
    assert "private-test-key" not in caplog.text and hit["source_text"] not in caplog.text


async def test_adapter_omits_only_optional_neighbors_as_whole_blocks(app):
    payload = response()
    hit = payload["hits"][0]
    primary = hit["context_parts"][0]
    required = {**primary, "chunk_id": "55555555-5555-4555-8555-555555555556",
                "source_text": "Only with sealed packaging.", "role": "condition", "required": True}
    optional = {**primary, "chunk_id": "55555555-5555-4555-8555-555555555557",
                "source_text": "Unrelated neighboring background. " * 500,
                "role": None, "required": False}
    hit["context_parts"] = [primary, required, optional]
    hit["context"] = "\n\n".join(part["source_text"] for part in hit["context_parts"])
    result, ctx, _ = await invoke(app, payload)
    citation = ctx.evidence["C1"]
    assert citation.content == hit["source_text"]
    assert citation.context_parts == [primary, required]
    assert required["source_text"] in citation.context
    assert optional["source_text"] not in citation.context
    assert citation.context_state == "truncated" and citation.context_omitted
    assert result["adapter_omissions"] == {"payload_budget": 0, "optional_context": 1}
    assert len(json.dumps(result).encode()) <= CueKBAdapter.result_budget_bytes


async def test_oversized_required_group_is_omitted_without_leaking_partial_evidence(app):
    payload = response()
    hit = payload["hits"][0]
    required = copy.deepcopy(hit["context_parts"][0])
    required.update(chunk_id="55555555-5555-4555-8555-555555555558",
                    source_text="Necessary packaging condition. " * 600,
                    role="condition", required=True)
    hit["context_parts"].append(required)
    hit["context"] = "\n\n".join(part["source_text"] for part in hit["context_parts"])
    result, ctx, _ = await invoke(app, payload)
    assert not result["hits"] and not ctx.evidence
    assert result["status"] == "ok"  # Preserve retrieval result; native consumer sees omission cause.
    assert result["hits_omitted"] == 1
    assert result["adapter_omissions"]["payload_budget"] == 1
    assert ctx.retrievals[0]["application_limited"] is True


async def test_degraded_empty_response_remains_distinct_from_not_found(app):
    payload = response()
    payload.update(retrieval_status="degraded", hits=[], degraded_reasons=["vector_unavailable"])
    payload["diagnostics"]["counts"].update(authoritative=0, returned=0)
    result, ctx, _ = await invoke(app, payload)
    assert result["status"] == "degraded" and result["degraded_reasons"] == ["vector_unavailable"]
    assert not ctx.evidence


async def test_old_v1_shape_is_rejected_without_implicit_schema_defaults(app):
    payload = response()
    for field in ("schema_version", "ranking_method", "ranking_version", "diagnostics"):
        payload.pop(field)
    with pytest.raises(ValidationError):
        await invoke(app, payload)


@pytest.mark.parametrize(("remote_code", "local_code"), [
    ("INDEX_UPGRADE_REQUIRED", "CUEKB_INDEX_UPGRADE_REQUIRED"),
    ("SEARCH_CONFIGURATION_CHANGED", "CUEKB_CONFIGURATION_CHANGED"),
    ("INDEX_MAINTENANCE_IN_PROGRESS", "CUEKB_INDEX_MAINTENANCE"),
    ("private-diagnostic-text", "CUEKB_CONFIGURATION_ERROR"),
])
async def test_v2_configuration_errors_are_bounded_safe_and_correlated(app, caplog, monkeypatch, remote_code, local_code):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    settings = app.state.settings
    settings.cuekb_mode, settings.cuekb_base_url = "real", "https://cuekb.fixture"
    settings.cuekb_api_key = SecretStr("private-test-key")
    requests = []
    trace_id = "22222222-2222-4222-8222-222222222222"
    def handle(request):
        requests.append(request)
        return httpx.Response(409, headers={"X-Trace-ID": trace_id}, json={
            "error_code": remote_code, "detail": "password=not-for-logs", "trace_id": trace_id})
    with caplog.at_level("INFO"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(DomainError) as error:
                await CueKBAdapter(settings, client).invoke(CueKBSearchInput(query="Product A"), context())
    assert len(requests) == 1
    assert error.value.code == local_code and error.value.retryable is False
    assert trace_id in caplog.text
    assert "not-for-logs" not in caplog.text and "private-diagnostic-text" not in caplog.text
