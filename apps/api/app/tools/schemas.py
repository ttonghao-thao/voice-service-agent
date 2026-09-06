from typing import Any, Literal

from app.contracts import Citation, StrictModel
from pydantic import Field


class RagInput(StrictModel):
    query: str = Field(min_length=1, max_length=2000)


class RagHit(StrictModel):
    document_id: str = Field(min_length=1, max_length=256)
    chunk_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=6000)
    score: float
    source_uri: str | None
    version: str = Field(min_length=1, max_length=128)
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagResponse(StrictModel):
    request_id: str
    retrieval_id: str
    status: Literal["ok", "conflict"]
    hits: list[RagHit] = Field(max_length=10)


class WeatherInput(StrictModel):
    place: str = Field(min_length=1, max_length=200)
    date: str | None = Field(default=None, max_length=32)
    units: Literal["metric", "imperial"] = "metric"


class Place(StrictModel):
    id: str
    name: str
    country_code: str
    timezone: str


class PlacesResponse(StrictModel):
    places: list[Place] = Field(max_length=10)


class Temperature(StrictModel):
    value: float = Field(allow_inf_nan=False)
    unit: Literal["C", "F"]


class WeatherSource(StrictModel):
    provider: str
    is_mock: bool


class WeatherResponse(StrictModel):
    status: Literal["ok"]
    place: Place
    kind: Literal["current", "forecast"]
    valid_at: str
    fetched_at: str
    temperature: Temperature
    condition: str = Field(max_length=500)
    source: WeatherSource
    stale: bool


class WeatherClarification(StrictModel):
    status: Literal["needs_clarification"]
    message: str
    places: list[Place] = Field(default_factory=list)
    is_mock: bool = False


class RagToolResult(StrictModel):
    status: Literal["ok", "insufficient_evidence", "conflict"]
    hits: list[Citation] = Field(default_factory=list)
    retrieval_id: str
