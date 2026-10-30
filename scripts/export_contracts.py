"""Generate versioned API/schema artifacts from application models. Run from repository root."""

import json
from pathlib import Path

from app.agent_runtime.tool_catalog import NativeToolCatalogUpdate, NativeToolEntry
from app.contracts import (
    AnswerBundle,
    CueKBArguments,
    KnowledgeArguments,
    KnowledgeInteractionArguments,
    portal_client_event_adapter,
    portal_server_event_adapter,
)
from app.main import create_app
from app.tools.evidence_contract import (
    EvidenceQueryRequest,
    EvidenceQueryResponse,
    EvidenceValidateRequest,
    EvidenceValidateResponse,
)
from app.tools.registry import ToolSpec
from app.tools.schemas import (
    CueKBSearchInput,
    CueKBSearchRequest,
    CueKBSearchResponse,
    PlacesResponse,
    WeatherInput,
    WeatherResponse,
)

root = Path(__file__).resolve().parents[1]
models = {
    "answer-bundle": AnswerBundle,
    "voice-knowledge-arguments": KnowledgeArguments,
    "voice-cuekb-arguments": CueKBArguments,
    "voice-knowledge-interaction-arguments": KnowledgeInteractionArguments,
    "tool-spec": ToolSpec,
    "voice-tool-entry": NativeToolEntry,
    "voice-tool-catalog-update": NativeToolCatalogUpdate,
    "cuekb-tool-query": CueKBSearchInput,
    "cuekb-search-request": CueKBSearchRequest,
    "cuekb-search-response": CueKBSearchResponse,
    "cuekb-evidence-query-v2.1-consumer-request": EvidenceQueryRequest,
    "cuekb-evidence-query-v2.1-consumer-response": EvidenceQueryResponse,
    "cuekb-evidence-validate-v2.1-consumer-request": EvidenceValidateRequest,
    "cuekb-evidence-validate-v2.1-consumer-response": EvidenceValidateResponse,
    "weather-query": WeatherInput,
    "weather-response": WeatherResponse,
    "places-response": PlacesResponse,
}
for name, model in models.items():
    (root / f"contracts/{name}.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"
    )
for name, adapter in {
    "portal-events": portal_server_event_adapter,
    "portal-client-events": portal_client_event_adapter,
}.items():
    (root / f"contracts/{name}.schema.json").write_text(
        json.dumps(adapter.json_schema(), indent=2, ensure_ascii=False) + "\n"
    )
(root / "contracts/openapi.json").write_text(
    json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"
)
