"""Generate versioned API/schema artifacts from application models. Run from repository root."""

import json
from pathlib import Path

from app.contracts import (
    AnswerBundle,
    portal_client_event_adapter,
    portal_server_event_adapter,
)
from app.main import create_app
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
    "tool-spec": ToolSpec,
    "cuekb-tool-query": CueKBSearchInput,
    "cuekb-search-request": CueKBSearchRequest,
    "cuekb-search-response": CueKBSearchResponse,
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
