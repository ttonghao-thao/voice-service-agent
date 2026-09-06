"""Generate versioned API/schema artifacts from application models. Run from repository root."""

import json
from pathlib import Path

from app.contracts import AnswerBundle, PortalEvent
from app.main import create_app
from app.tools.registry import ToolSpec
from app.tools.schemas import PlacesResponse, RagInput, RagResponse, WeatherInput, WeatherResponse

root = Path(__file__).resolve().parents[1]
models = {
    "portal-events": PortalEvent,
    "answer-bundle": AnswerBundle,
    "tool-spec": ToolSpec,
    "rag-query": RagInput,
    "rag-response": RagResponse,
    "weather-query": WeatherInput,
    "weather-response": WeatherResponse,
    "places-response": PlacesResponse,
}
for name, model in models.items():
    (root / f"contracts/{name}.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"
    )
(root / "contracts/openapi.json").write_text(
    json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"
)
